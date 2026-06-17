#!/usr/bin/env python3
"""Provider-agnostic MCP (Model Context Protocol) host.

Jarvis is the MCP *client*: it spawns configured MCP servers, performs the JSON-RPC handshake,
lists their tools, and calls them — independent of which LLM backend is answering. The tools are
merged into the same tool registry Jarvis already exposes (chat_tools), so an HTTP backend gets
them as native tool_calls and a CLI backend gets them via the text-recovery path. This is the
deliberate design: MCP support must NOT be tied to a single provider (see RoutingLLM/Brain.turn).

stdlib only. stdio transport (newline-delimited JSON-RPC 2.0). Degrade-safe: a server that fails
to start/respond is skipped, never crashing a chat turn. Bounded: every RPC has a timeout.

Tool names are namespaced `mcp__<server>__<tool>` so multiple servers can't collide and the
dispatcher can route a call back to the owning server.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import logging

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"     # widely supported MCP revision
_PREFIX = "mcp__"
_CONNECT_TIMEOUT = 20               # seconds to come up + handshake + list tools
_CALL_TIMEOUT = 60                 # seconds for a single tools/call
_MAX_RESULT = 16000                # cap tool output fed back to the model

_CLIENTS: dict[str, "MCPClient"] = {}
_LOCK = threading.RLock()


def _san(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(name or "")).strip("_")


def servers_from_cfg(cfg: dict) -> list[dict]:
    """Enabled stdio MCP servers from config: mcp.servers = [{name, command, args, env, enabled}]."""
    mcp = (cfg or {}).get("mcp") or {}
    out = []
    for s in (mcp.get("servers") or []):
        if not isinstance(s, dict):
            continue
        if s.get("enabled") is False:
            continue
        if not s.get("name") or not s.get("command"):
            continue
        out.append(s)
    return out


class MCPClient:
    """One stdio MCP server: a long-lived subprocess spoken to over newline-delimited JSON-RPC.
    A background reader thread parses incoming messages and unblocks the matching request id, so
    server-initiated notifications/logs never desync the request/response pairing."""

    def __init__(self, spec: dict) -> None:
        self.name = _san(spec.get("name"))
        self.command = spec.get("command")
        self.args = list(spec.get("args") or [])
        self.env = spec.get("env") or {}
        self.proc: subprocess.Popen | None = None
        self.tools: list[dict] = []
        self.error: str = ""
        self._id = 0
        self._pending: dict[int, dict] = {}
        self._wlock = threading.Lock()
        self._reader: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------
    def _start(self) -> bool:
        if self.proc and self.proc.poll() is None:
            return True
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in self.env.items()})
        try:
            self.proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1, env=env,
            )
        except Exception as e:
            self.error = f"spawn failed: {str(e)[:160]}"
            return False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        return True

    def _read_loop(self) -> None:
        try:
            for line in self.proc.stdout:            # blocks; ends when the child closes stdout
                line = (line or "").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                mid = msg.get("id")
                if mid is None:                       # a notification/log — ignore
                    continue
                slot = self._pending.get(mid)
                if slot is not None:
                    slot["result"] = msg
                    slot["event"].set()
        except Exception:
            pass

    def _rpc(self, method: str, params: dict | None, timeout: float, notify: bool = False) -> dict:
        if not (self.proc and self.proc.poll() is None):
            raise RuntimeError("mcp server not running")
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if notify:
            with self._wlock:
                self.proc.stdin.write(json.dumps(msg) + "\n"); self.proc.stdin.flush()
            return {}
        self._id += 1
        mid = self._id
        msg["id"] = mid
        ev = threading.Event()
        self._pending[mid] = {"event": ev, "result": None}
        with self._wlock:
            self.proc.stdin.write(json.dumps(msg) + "\n"); self.proc.stdin.flush()
        if not ev.wait(timeout):
            self._pending.pop(mid, None)
            raise TimeoutError(f"mcp {method} timed out after {timeout}s")
        res = self._pending.pop(mid, {}).get("result") or {}
        if isinstance(res.get("error"), dict):
            raise RuntimeError(f"mcp {method} error: {str(res['error'].get('message'))[:200]}")
        return res.get("result") or {}

    def connect(self) -> bool:
        """Start + handshake + list tools. Caches self.tools. Returns True on success."""
        with _LOCK:
            if self.tools:                            # already connected this process
                return True
            if not self._start():
                return False
            try:
                self._rpc("initialize", {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "jarvis", "version": "1"},
                }, timeout=_CONNECT_TIMEOUT)
                self._rpc("notifications/initialized", {}, timeout=_CONNECT_TIMEOUT, notify=True)
                listed = self._rpc("tools/list", {}, timeout=_CONNECT_TIMEOUT)
                self.tools = [t for t in (listed.get("tools") or []) if t.get("name")]
                self.error = ""
                return True
            except Exception as e:
                self.error = str(e)[:200]
                self.close()
                return False

    def call_tool(self, tool: str, arguments: dict) -> dict:
        if not self.connect():
            return {"ok": False, "error": self.error or "mcp server unavailable"}
        try:
            res = self._rpc("tools/call", {"name": tool, "arguments": arguments or {}}, timeout=_CALL_TIMEOUT)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}
        # MCP returns content as a list of blocks ({type:text|image|...}); flatten text for the model.
        parts = []
        for block in (res.get("content") or []):
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif block.get("type") == "image":
                    parts.append("[image returned]")
        text = "\n".join(p for p in parts if p).strip()
        return {"ok": not res.get("isError"), "text": text[:_MAX_RESULT]}

    def close(self) -> None:
        p = self.proc
        self.proc = None
        if p and p.poll() is None:
            try:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except Exception:
                    p.kill()
            except Exception:
                pass


# -- module-level manager (cached clients per process) --------------------
def _client(spec: dict) -> "MCPClient":
    name = _san(spec.get("name"))
    with _LOCK:
        c = _CLIENTS.get(name)
        if c is None:
            c = MCPClient(spec)
            _CLIENTS[name] = c
        return c


def tool_specs(cfg: dict) -> list[dict]:
    """OpenAI-format function specs for every tool across enabled MCP servers, namespaced so the
    dispatcher can route the call. Connections are cached, so this is cheap after the first turn.
    Returns [] (no spawning) when no servers are configured."""
    specs: list[dict] = []
    for spec in servers_from_cfg(cfg):
        c = _client(spec)
        if not c.connect():
            continue
        for t in c.tools:
            specs.append({"type": "function", "function": {
                "name": f"{_PREFIX}{c.name}__{_san(t['name'])}",
                "description": (t.get("description") or f"{t['name']} (via MCP server {c.name})")[:1024],
                "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
            }})
    return specs


def is_mcp_tool(name: str) -> bool:
    return str(name or "").startswith(_PREFIX)


def call(full_name: str, arguments: dict, cfg: dict | None = None) -> dict:
    """Route a namespaced `mcp__<server>__<tool>` call to its server. cfg is loaded lazily if the
    server isn't already a cached/connected client (e.g. the loop process's first MCP call)."""
    rest = str(full_name or "")[len(_PREFIX):]
    server, _, tool = rest.partition("__")
    server = _san(server)
    with _LOCK:
        c = _CLIENTS.get(server)
    if c is None:
        if cfg is None:
            try:
                from jarvis.config import load
                cfg = load()
            except Exception:
                cfg = {}
        spec = next((s for s in servers_from_cfg(cfg) if _san(s.get("name")) == server), None)
        if not spec:
            return {"ok": False, "error": f"unknown MCP server '{server}'"}
        c = _client(spec)
    return c.call_tool(tool, arguments or {})


def catalog(cfg: dict) -> str:
    """Compact catalog of MCP tools for prompt injection so any model detects them."""
    lines = []
    for spec in servers_from_cfg(cfg):
        c = _client(spec)
        if not c.connect():
            continue
        for t in c.tools:
            desc = (t.get("description") or "").strip().replace("\n", " ")
            lines.append(f"- {_PREFIX}{c.name}__{_san(t['name'])} — {desc}"[:240])
    return "\n".join(lines)


def probe(cfg: dict) -> list[dict]:
    """Per-server status + discovered tool names, for the settings UI."""
    out = []
    for spec in servers_from_cfg(cfg):
        c = _client(spec)
        ok = c.connect()
        out.append({
            "name": c.name,
            "command": spec.get("command"),
            "args": spec.get("args") or [],
            "connected": ok,
            "error": c.error,
            "tools": [t.get("name") for t in c.tools] if ok else [],
        })
    return out


def shutdown() -> None:
    with _LOCK:
        for c in _CLIENTS.values():
            c.close()
        _CLIENTS.clear()
