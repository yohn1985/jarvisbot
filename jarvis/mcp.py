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

import base64
import hashlib
import json
import os
import re
import secrets as _secrets
import subprocess
import threading
import time
import logging
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"     # widely supported MCP revision
_PREFIX = "mcp__"
_CONNECT_TIMEOUT = 20               # seconds to come up + handshake + list tools
_CALL_TIMEOUT = 60                 # seconds for a single tools/call
_MAX_RESULT = 16000                # cap tool output fed back to the model

_ROOT = Path(__file__).resolve().parent.parent
_AUTH_DIR = _ROOT / "state" / "mcp-auth"      # persisted OAuth tokens (chmod 600; NOT in config.yaml)
# Default OAuth redirect = loopback, so the dashboard never needs to be exposed or have a cert.
# The provider only redirects the BROWSER here; it never connects inbound. Override via mcp.oauth.
# Use the hostname "localhost", NOT the IP 127.0.0.1: many OAuth servers (e.g. Clerk/Higgsfield)
# allow an http redirect only for hosts with the "localhost" suffix and reject the bare IP.
_DEFAULT_REDIRECT = "http://localhost:8787/api/mcp/oauth/callback"
_PENDING: dict[str, dict] = {}                # state -> in-flight auth (verifier, endpoints, ...)

_CLIENTS: dict[str, "MCPClient"] = {}
_LOCK = threading.RLock()

# Secret references in a server's `env`: ${env:NAME} / ${NAME} / $NAME resolve from the process
# environment at spawn time, so config.yaml holds only the REFERENCE, never the secret value.
# Populate the real value via Jarvis's .env, a systemd EnvironmentFile, or an Infisical wrapper —
# whatever injects it into the environment. Keeps the open-source core generic (no secret in-tree).
_ENV_REF = re.compile(r"\$\{(?:env:)?([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)")


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
        if not s.get("name") or not (s.get("command") or s.get("url")):
            continue
        out.append(s)
    return out


def _redirect_uri(cfg: dict) -> str:
    return str(((cfg or {}).get("mcp") or {}).get("oauth_redirect") or _DEFAULT_REDIRECT)


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _resolve_secret(value: str) -> str:
    """Resolve ${env:NAME}/$NAME refs from the environment, so a client_secret can be a reference
    (kept in .env) instead of sitting in config.yaml. Loads Jarvis's .env best-effort first."""
    s = str(value or "")
    if "$" not in s:
        return s
    try:
        from jarvis.bootstrap import secrets
        secrets.load_env()
    except Exception:
        pass
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1) or m.group(2), ""), s)


def _http(url: str, *, method: str = "GET", data=None, headers=None, timeout: int = 20):
    """Minimal stdlib HTTP returning (status, headers_dict, parsed_json_or_text). Never raises on
    HTTP error status — returns it so callers can branch (e.g. 401 -> needs auth)."""
    body = None
    hdrs = dict(headers or {})
    hdrs.setdefault("User-Agent", "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0")
    if data is not None:
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode(); hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(data, str):
            body = data.encode()
        else:
            body = data
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = resp.read().decode("utf-8", "replace")
        return resp.status, dict(resp.headers), _maybe_json(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, dict(e.headers or {}), _maybe_json(raw)
    except Exception as e:
        return 0, {}, str(e)


def _maybe_json(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        # streamable-HTTP may answer a POST with SSE: pull the JSON out of the last data: line
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except Exception:
                    continue
        return raw


# -- OAuth token store (per server) ---------------------------------------
def _tokens_path(name: str) -> Path:
    return _AUTH_DIR / f"{_san(name)}.json"


def _load_tokens(name: str) -> dict:
    try:
        return json.loads(_tokens_path(name).read_text())
    except Exception:
        return {}


def _save_tokens(name: str, data: dict) -> None:
    _AUTH_DIR.mkdir(parents=True, exist_ok=True)
    p = _tokens_path(name)
    p.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(p, 0o600)                          # tokens are secrets
    except Exception:
        pass


def forget_auth(name: str) -> None:
    try:
        _tokens_path(name).unlink(missing_ok=True)
    except Exception:
        pass


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
    def _resolve_env(self) -> tuple[dict, list]:
        """Resolve ${env:NAME}/$NAME references in the server's env from the process environment.
        Returns (resolved_env, missing_names). Secrets stay out of config.yaml — only refs live there."""
        try:                                  # best-effort: load Jarvis's .env into os.environ first
            from jarvis.bootstrap import secrets
            secrets.load_env()
        except Exception:
            pass
        missing: list[str] = []
        resolved: dict[str, str] = {}
        for k, v in (self.env or {}).items():
            def _sub(m):
                name = m.group(1) or m.group(2)
                val = os.environ.get(name)
                if val is None:
                    missing.append(name)
                    return ""
                return val
            resolved[str(k)] = _ENV_REF.sub(_sub, str(v))
        return resolved, missing

    def _start(self) -> bool:
        if self.proc and self.proc.poll() is None:
            return True
        env = dict(os.environ)
        custom, missing = self._resolve_env()
        if missing:                           # fail closed with a clear signal, not a silent bad-auth
            self.error = "missing env var(s): " + ", ".join(sorted(set(missing)))
            return False
        env.update(custom)
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


class _AuthRequired(Exception):
    pass


def _discover_oauth(mcp_url: str, www_authenticate: str = "") -> dict:
    """Discover the server's OAuth metadata: resource metadata (RFC 9728) -> authorization server
    metadata (RFC 8414 / OIDC). Returns the auth-server metadata dict (authorization_endpoint,
    token_endpoint, registration_endpoint, scopes_supported)."""
    parsed = urllib.parse.urlparse(mcp_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    auth_servers = []
    m = re.search(r'resource_metadata="?([^",\s]+)"?', www_authenticate or "")
    for prm in ([m.group(1)] if m else []) + [origin + "/.well-known/oauth-protected-resource"]:
        st, _h, j = _http(prm, timeout=15)
        if isinstance(j, dict) and j.get("authorization_servers"):
            auth_servers = j["authorization_servers"]; break
    auth_base = (auth_servers[0] if auth_servers else origin).rstrip("/")
    ap = urllib.parse.urlparse(auth_base)
    a_origin = f"{ap.scheme}://{ap.netloc}"
    a_path = ap.path.rstrip("/")                  # e.g. "/ads" (Meta) or "" (Higgsfield)
    cands = []
    for wk in ("oauth-authorization-server", "openid-configuration"):
        cands.append(f"{a_origin}/.well-known/{wk}{a_path}")   # RFC 8414: well-known at root + path
        cands.append(f"{auth_base}/.well-known/{wk}")          # path-then-well-known variant
    for c in dict.fromkeys(cands):                # dedupe (collapse when a_path == "")
        st, _h, j = _http(c, timeout=15)
        if isinstance(j, dict) and j.get("authorization_endpoint") and j.get("token_endpoint"):
            return j
    return {}


def _register_client(meta: dict, redirect_uri: str) -> dict:
    """Dynamic client registration (RFC 7591) — so there's no manual developer-app setup."""
    reg = meta.get("registration_endpoint")
    if not reg:
        return {}
    st, _h, j = _http(reg, method="POST", timeout=20, data={
        "client_name": "Jarvis", "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none",
    })
    if isinstance(j, dict) and j.get("client_id"):
        return {"client_id": j["client_id"], "client_secret": j.get("client_secret", "")}
    return {}


def begin_auth(cfg: dict, name: str, base_redirect: str | None = None) -> dict:
    """Start the OAuth Connect flow for an http MCP server: discover, register, build the authorize
    URL (PKCE). The dashboard opens that URL; the provider redirects the BROWSER back to the callback.
    Returns {ok, authorize_url}.

    The redirect MUST be an address the user's browser can actually reach. The provider redirects the
    BROWSER (not Jarvis) there, so loopback (127.0.0.1) only works when the browser runs on the same
    host as the dashboard. When the dashboard is opened over the LAN, the caller passes the request's
    own scheme+Host as `base_redirect` so the browser is sent back to the same origin it's already on.
    Precedence: an explicit config `mcp.oauth_redirect` > the request-derived `base_redirect` > loopback."""
    raw = ((cfg.get("mcp") or {}).get("servers")) or []
    spec = next((s for s in raw if _san(s.get("name")) == _san(name)), None)
    if not spec or not spec.get("url"):
        return {"ok": False, "error": "not an HTTP MCP server"}
    url = spec["url"]
    redirect = (((cfg or {}).get("mcp") or {}).get("oauth_redirect")
                or base_redirect or _DEFAULT_REDIRECT)
    # Normalize the loopback IP to the "localhost" hostname: equivalent for routing, but OAuth servers
    # that gate http redirects on a "localhost" suffix reject the bare 127.0.0.1 form.
    redirect = redirect.replace("//127.0.0.1:", "//localhost:").replace("//127.0.0.1/", "//localhost/")
    st, hdrs, _b = _http(url, method="POST", timeout=15, headers={"Accept": "application/json, text/event-stream"},
                         data={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                          "clientInfo": {"name": "jarvis", "version": "1"}}})
    meta = _discover_oauth(url, hdrs.get("WWW-Authenticate", "") if st == 401 else "")
    if not meta.get("authorization_endpoint") or not meta.get("token_endpoint"):
        return {"ok": False, "error": "could not discover the server's OAuth endpoints"}
    saved = _load_tokens(name)
    # Pick the OAuth client in priority order:
    #  1. An explicitly configured client_id (e.g. a Meta App ID — Meta forbids dynamic registration).
    #     The user owns that app's redirect-URI allowlist, so we never re-register it.
    #  2. A previously DCR-registered client, BUT only if it was registered for THIS redirect. A saved
    #     client registered under a different redirect (port/host changed, or an old build) makes the
    #     provider reject the authorize call with "redirect_uri does not match" — so re-register instead.
    #  3. Otherwise, dynamic client registration (RFC 7591) against the current redirect.
    if spec.get("client_id"):
        client = {"client_id": spec["client_id"],
                  "client_secret": _resolve_secret(spec.get("client_secret") or saved.get("client_secret", ""))}
    elif saved.get("client_id") and saved.get("redirect") == redirect:
        client = {"client_id": saved["client_id"],
                  "client_secret": _resolve_secret(saved.get("client_secret", ""))}
    else:
        client = _register_client(meta, redirect)
    if not client.get("client_id"):
        return {"ok": False, "error": "this server requires a registered app — set a client_id on the "
                "server (e.g. your Meta App ID) and add the redirect URI to that app: " + redirect}
    verifier = _b64url(_secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = _b64url(_secrets.token_bytes(24))
    params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect,
              "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "resource": url}
    scope = spec.get("scope") or " ".join(meta.get("scopes_supported") or [])
    if scope:
        params["scope"] = scope
    sep = "&" if "?" in meta["authorization_endpoint"] else "?"
    authorize_url = meta["authorization_endpoint"] + sep + urllib.parse.urlencode(params)
    _PENDING[state] = {"name": name, "verifier": verifier, "redirect": redirect,
                       "token_endpoint": meta["token_endpoint"], "client": client, "resource": url}
    saved.update({"client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
                  "redirect": redirect})   # remember the redirect this client was registered for
    _save_tokens(name, saved)
    return {"ok": True, "authorize_url": authorize_url}


def complete_auth(state: str, code: str) -> dict:
    """Handle the loopback callback: exchange the code (+PKCE verifier) for tokens and persist them."""
    p = _PENDING.pop(str(state), None)
    if not p:
        return {"ok": False, "error": "unknown or expired auth state"}
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": p["redirect"],
            "client_id": p["client"]["client_id"], "code_verifier": p["verifier"], "resource": p["resource"]}
    if p["client"].get("client_secret"):
        data["client_secret"] = p["client"]["client_secret"]
    st, _h, j = _http(p["token_endpoint"], method="POST", timeout=20,
                      data=urllib.parse.urlencode(data),
                      headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    if not isinstance(j, dict) or not j.get("access_token"):
        return {"ok": False, "error": f"token exchange failed (HTTP {st})"}
    saved = _load_tokens(p["name"])
    saved.update({"client_id": p["client"]["client_id"], "client_secret": p["client"].get("client_secret", ""),
                  "access_token": j["access_token"], "expires_at": time.time() + int(j.get("expires_in", 3600)),
                  "refresh_token": j.get("refresh_token", saved.get("refresh_token", "")),
                  "token_endpoint": p["token_endpoint"], "resource": p["resource"]})
    _save_tokens(p["name"], saved)
    with _LOCK:
        _CLIENTS.pop(_san(p["name"]), None)        # drop cache so it reconnects with the token
    return {"ok": True, "name": p["name"]}


class MCPHttpClient:
    """A remote MCP server over streamable HTTP (JSON-RPC POST; json or SSE responses). OAuth Bearer
    is attached from the persisted token store and auto-refreshed. Same interface as MCPClient."""

    def __init__(self, spec: dict) -> None:
        self.name = _san(spec.get("name"))
        self.url = spec.get("url")
        self.spec = spec
        self.tools: list[dict] = []
        self.error = ""
        self.needs_auth = False
        self.session = ""
        self._id = 0

    def _token(self) -> str:
        t = _load_tokens(self.name)
        if not t.get("access_token"):
            return ""
        if t.get("expires_at") and time.time() > float(t["expires_at"]) - 60:
            self._refresh(t)
            t = _load_tokens(self.name)
        return t.get("access_token", "")

    def _refresh(self, t: dict) -> None:
        if not (t.get("refresh_token") and t.get("token_endpoint")):
            return
        data = {"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": t.get("client_id", "")}
        if t.get("client_secret"):
            data["client_secret"] = t["client_secret"]
        if t.get("resource"):
            data["resource"] = t["resource"]
        st, _h, j = _http(t["token_endpoint"], method="POST", timeout=20, data=urllib.parse.urlencode(data),
                          headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        if isinstance(j, dict) and j.get("access_token"):
            t["access_token"] = j["access_token"]; t["expires_at"] = time.time() + int(j.get("expires_in", 3600))
            if j.get("refresh_token"):
                t["refresh_token"] = j["refresh_token"]
            _save_tokens(self.name, t)

    def _rpc(self, method: str, params, timeout: float, notify: bool = False):
        self._id += 1
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = self._id
        hdrs = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL_VERSION}
        tok = self._token()
        if tok:
            hdrs["Authorization"] = f"Bearer {tok}"
        if self.session:
            hdrs["Mcp-Session-Id"] = self.session
        st, rh, j = _http(self.url, method="POST", data=msg, headers=hdrs, timeout=timeout)
        if st == 401:
            raise _AuthRequired(rh.get("WWW-Authenticate", ""))
        sid = rh.get("Mcp-Session-Id")
        if sid:
            self.session = sid
        if notify:
            return {}
        if isinstance(j, dict) and isinstance(j.get("error"), dict):
            raise RuntimeError(str(j["error"].get("message"))[:200])
        return (j.get("result") or {}) if isinstance(j, dict) else {}

    def connect(self) -> bool:
        with _LOCK:
            if self.tools:
                return True
            try:
                self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                         "clientInfo": {"name": "jarvis", "version": "1"}}, timeout=_CONNECT_TIMEOUT)
                self._rpc("notifications/initialized", {}, timeout=_CONNECT_TIMEOUT, notify=True)
                listed = self._rpc("tools/list", {}, timeout=_CONNECT_TIMEOUT)
                self.tools = [t for t in (listed.get("tools") or []) if t.get("name")]
                self.needs_auth = False; self.error = ""
                return True
            except _AuthRequired:
                self.needs_auth = True; self.error = "not connected — click Connect to authorize"
                return False
            except Exception as e:
                self.error = str(e)[:200]
                return False

    def call_tool(self, tool: str, arguments: dict) -> dict:
        if not self.connect():
            return {"ok": False, "error": self.error or "mcp server unavailable"}
        try:
            res = self._rpc("tools/call", {"name": tool, "arguments": arguments or {}}, timeout=_CALL_TIMEOUT)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}
        parts = [str(b.get("text") or "") for b in (res.get("content") or [])
                 if isinstance(b, dict) and b.get("type") == "text"]
        return {"ok": not res.get("isError"), "text": "\n".join(p for p in parts if p)[:_MAX_RESULT]}

    def close(self) -> None:
        pass


# -- module-level manager (cached clients per process) --------------------
def _client(spec: dict):
    name = _san(spec.get("name"))
    with _LOCK:
        c = _CLIENTS.get(name)
        if c is None:
            c = MCPHttpClient(spec) if spec.get("url") else MCPClient(spec)
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


def catalog(cfg: dict, for_cli: bool = False) -> str:
    """Compact catalog of MCP tools for prompt injection so any model detects them. When `for_cli` (the
    answering backend is the Claude CLI), only advertise servers the CLI is actually given via
    --mcp-config — so we never list a tool the CLI can't call (see _expose_to_cli)."""
    lines = []
    for spec in servers_from_cfg(cfg):
        if for_cli and not _expose_to_cli(spec):
            continue
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
            "url": spec.get("url"),
            "transport": "http" if spec.get("url") else "stdio",
            "connected": ok,
            "needs_auth": getattr(c, "needs_auth", False),
            "error": c.error,
            "tools": [t.get("name") for t in c.tools] if ok else [],
        })
    return out


def _fresh_bearer(name: str) -> str:
    """Current access token for an HTTP MCP server, refreshed if within 60s of expiry. '' if not authed.
    Used to inject a live Bearer into a config we hand to another agent (e.g. the Claude CLI)."""
    t = _load_tokens(name)
    if not t.get("access_token"):
        return ""
    if (t.get("expires_at") and time.time() > float(t["expires_at"]) - 60
            and t.get("refresh_token") and t.get("token_endpoint")):
        data = {"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": t.get("client_id", "")}
        if t.get("client_secret"):
            data["client_secret"] = t["client_secret"]
        if t.get("resource"):
            data["resource"] = t["resource"]
        st, _h, j = _http(t["token_endpoint"], method="POST", timeout=20, data=urllib.parse.urlencode(data),
                          headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        if isinstance(j, dict) and j.get("access_token"):
            t["access_token"] = j["access_token"]; t["expires_at"] = time.time() + int(j.get("expires_in", 3600))
            if j.get("refresh_token"):
                t["refresh_token"] = j["refresh_token"]
            _save_tokens(name, t)
    return t.get("access_token", "")


def _expose_to_cli(spec: dict) -> bool:
    """Whether to hand this MCP server to the Claude CLI. Remote (HTTP) servers: yes — the CLI can't
    reach them otherwise. Local stdio servers: NO by default — the CLI already has native file/shell
    tools, and re-spawning a stdio server (e.g. `npx` filesystem) every turn adds latency. Set
    `cli: true` on a stdio server to force-include one the CLI genuinely needs."""
    if spec.get("url"):
        return True
    return bool(spec.get("cli"))


def claude_cli_config(cfg: dict) -> tuple[dict, list[str]]:
    """Build a Claude Code CLI `--mcp-config` (an {"mcpServers": {...}} map) plus the allowedTools list,
    so the `claude` CLI backend can use the SAME MCP servers Jarvis is configured with. The CLI is its
    own agent and can't see Jarvis's in-process tool registry, so we hand it a native config instead:
      - HTTP servers get the stored OAuth Bearer injected (refreshed) — no second OAuth dance.
      - stdio servers get their command/args + env (with ${env:..} secret refs resolved at spawn).
    Returns ({"mcpServers": {...}}, ["mcp__<server>", ...]); the allowedTools entries pre-approve every
    tool from each server so the CLI can call them non-interactively in -p mode."""
    servers: dict = {}
    allowed: list[str] = []
    for spec in servers_from_cfg(cfg):
        if not _expose_to_cli(spec):                       # stdio servers the CLI already covers (e.g.
            continue                                       # filesystem) are skipped — see _expose_to_cli
        name = _san(spec.get("name"))
        if spec.get("url"):
            tok = _fresh_bearer(name)
            if not tok:
                continue                                  # not authorized yet -> the CLI can't use it
            entry = {"type": "http", "url": spec["url"], "headers": {"Authorization": f"Bearer {tok}"}}
        elif spec.get("command"):
            entry = {"command": spec["command"], "args": [str(a) for a in (spec.get("args") or [])]}
            env = {str(k): _resolve_secret(str(v)) for k, v in (spec.get("env") or {}).items()}
            if env:
                entry["env"] = env
        else:
            continue
        servers[name] = entry
        allowed.append(f"{_PREFIX}{name}")                # mcp__<server> = allow all tools from that server
    return {"mcpServers": servers}, allowed


def test(cfg: dict, name: str) -> dict:
    """Force a FRESH connect+probe of one server (drop the cached client first) and report health —
    a manual check from the UI without a chat turn (after enabling credits, rotating a token, etc.)."""
    name = _san(name)
    with _LOCK:
        c = _CLIENTS.pop(name, None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
    spec = next((s for s in servers_from_cfg(cfg) if _san(s.get("name")) == name), None)
    if not spec:
        return {"ok": False, "name": name, "error": "unknown or disabled server"}
    c = _client(spec)
    ok = c.connect()
    return {"ok": ok, "name": name, "connected": ok, "transport": "http" if spec.get("url") else "stdio",
            "needs_auth": getattr(c, "needs_auth", False),
            "tools": [t.get("name") for t in c.tools] if ok else [],
            "error": c.error}


def shutdown() -> None:
    with _LOCK:
        for c in _CLIENTS.values():
            c.close()
        _CLIENTS.clear()
