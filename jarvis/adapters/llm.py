"""LLM provider router (the mind): role -> "backend:model", dispatched to a CLI backend.
Generalizes ai-exec. Backends are CLI command templates in config ({model}/{prompt}; no
{prompt} placeholder => prompt is piped on stdin). A routed backend that's absent or fails
falls through to `fallbacks`. Stdlib only."""
from __future__ import annotations
import json, os, shutil, subprocess, tempfile, urllib.request
from pathlib import Path

DEFAULT_BACKENDS = {
    "claude": ["claude", "-p", "--model", "{model}"],
    "codex":  ["codex", "exec", "--model", "{model}", "--cd", "{workspace}",
               "--sandbox", "danger-full-access", "-c", "approval_policy=\"never\"",
               "--skip-git-repo-check"],
    "ollama": ["ollama", "run", "{model}", "{prompt}"],
}

# A backend may also be an HTTP (OpenAI-compatible) endpoint instead of a CLI list, e.g.:
#   ollama_cloud: {http: "https://ollama.com/v1/chat/completions", api_key_env: OLLAMA_API_KEY}
# This covers Ollama Cloud and any OpenAI-compatible API (subscription key, no local CLI needed).

# Reasoning controls, applied per role from llm.params (set in the dashboard). The capability model
# is the SINGLE source of truth for "which control + which options" per backend, shared by the
# dashboard (to render the chip) and the apply sites below (to send the right flag/param). See
# docs/reasoning-controls-research.md — the option sets differ per backend and were verified live:
#   claude CLI : --effort   low/medium/high/xhigh/max          (native flag; NOT a thinking keyword)
#   codex CLI  : -c model_reasoning_effort=  minimal/low/medium/high
#   openai http: reasoning_effort  minimal/low/medium/high
#   ollama http: reasoning_effort  low/medium/high  (only honored by models whose caps include 'thinking')
#   anthropic  : thinking budget_tokens  low/medium/high/max   (API-key path only)
EFFORT_CLAUDE = ["low", "medium", "high", "xhigh", "max"]
EFFORT_CODEX = ["minimal", "low", "medium", "high"]
EFFORT_OPENAI = ["minimal", "low", "medium", "high"]
EFFORT_OLLAMA = ["low", "medium", "high"]
THINKING_ANTHROPIC = ["low", "medium", "high", "max"]
_THINK_BUDGET = {"low": 4000, "medium": 10000, "high": 24000, "max": 32000}   # Anthropic thinking budget_tokens


def backend_kind(name: str, spec) -> str:
    """Classify a backend so capability/apply logic keys off WHAT it is, not just its config name.
    The same Claude model is 'effort' via the CLI but 'thinking' via the API — kind captures that."""
    if isinstance(spec, dict) and spec.get("http"):
        if spec.get("format") == "anthropic":
            return "anthropic_http"
        if "ollama" in (spec.get("http") or "").lower():
            return "ollama_http"
        return "openai_http"
    if name == "claude":
        return "claude_cli"
    if name == "codex":
        return "codex_cli"
    if name == "ollama":
        return "ollama_local"
    return "cli"


def reasoning_caps(kind: str, model_caps=None) -> dict:
    """The reasoning control a backend kind exposes: {"control": "effort"|"thinking"|None, "options":[...]}.
    For ollama, effort is real only when the model's /api/show caps include 'thinking' (else None)."""
    if kind == "claude_cli":
        return {"control": "effort", "options": EFFORT_CLAUDE}
    if kind == "codex_cli":
        return {"control": "effort", "options": EFFORT_CODEX}
    if kind == "openai_http":
        return {"control": "effort", "options": EFFORT_OPENAI}
    if kind == "anthropic_http":
        return {"control": "thinking", "options": THINKING_ANTHROPIC}
    if kind in ("ollama_http", "ollama_local"):
        if model_caps and "thinking" in model_caps:
            return {"control": "effort", "options": EFFORT_OLLAMA}
        return {"control": None, "options": []}
    return {"control": None, "options": []}


# Context-window selection. An ollama model runs at any num_ctx up to its max (verified: the cloud
# OpenAI endpoint honors options.num_ctx), so we offer standard sizes up to the model's max and apply
# the choice. Anthropic Sonnet has a 200K/1M(beta) split, but 1M needs an API key (the CLI refuses
# --betas), so it's only offered on the anthropic_http backend. Everything else is a single fixed window.
_CTX_STEPS = [16384, 32768, 65536, 131072, 262144, 524288, 1048576]


def context_options(kind: str, max_window: int, model: str = "") -> list:
    """Selectable context sizes (ints) for a backend/model. [single] when there's nothing to pick."""
    mw = int(max_window or 0)
    if kind in ("ollama_http", "ollama_local") and mw:
        return sorted(set([s for s in _CTX_STEPS if s < mw] + [mw]))
    if kind == "anthropic_http" and "sonnet" in (model or "").lower():
        return [200000, 1000000]
    return [mw] if mw else []


def context_num_ctx(kind: str, params: dict) -> int:
    """The num_ctx to apply for an ollama request from per-role params (0 = leave the server default)."""
    if kind not in ("ollama_http", "ollama_local"):
        return 0
    try:
        return int((params or {}).get("context") or 0)
    except (TypeError, ValueError):
        return 0


def reasoning_value(kind: str, params: dict, model_caps=None) -> str:
    """Resolve the reasoning level to apply for this backend from per-role params. Prefers the control's
    own key (effort/thinking) but falls back to the other so a generic hint (e.g. think='medium' from the
    chat) reaches every backend. Returns '' when there's nothing valid to apply."""
    caps = reasoning_caps(kind, model_caps)
    if not caps["control"]:
        return ""
    val = str((params or {}).get(caps["control"]) or (params or {}).get("effort")
              or (params or {}).get("thinking") or "")
    return val if (val and val != "default" and val in caps["options"]) else ""


def _img_media_type(path):
    import os
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/png")


def _img_b64(path):
    import base64
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode(), _img_media_type(path)


class RoutingLLM:
    def __init__(self, routing, backends, aliases, fallbacks, params=None):
        self.routing = routing or {}
        self.backends = backends or DEFAULT_BACKENDS
        self.aliases = aliases or {}
        self.fallbacks = fallbacks or []
        self.params = params or {}            # role -> {"effort": .., "thinking": ..}

    def _resolve(self, target):
        backend, _, model = (target or "").partition(":")
        return backend, self.aliases.get(model, model)

    def _present(self, backend):
        spec = self.backends.get(backend)
        if isinstance(spec, dict) and spec.get("http"):       # HTTP backend: present if its key is set
            env = spec.get("api_key_env")
            return bool(spec["http"]) and (not env or bool(os.environ.get(env)))
        return bool(spec) and shutil.which(spec[0]) is not None

    def _invoke(self, backend, model, prompt, timeout, params=None):
        params = params or {}
        spec = self.backends[backend]
        if isinstance(spec, dict) and spec.get("http"):
            return self._invoke_http(spec, model, prompt, timeout, params)
        workspace = os.environ.get("JARVIS_CODEX_CWD") or os.environ.get("JARVIS_WORKSPACE") or os.getcwd()
        if not Path(workspace).exists():
            workspace = os.getcwd()
        cmd = [a.replace("{model}", model).replace("{workspace}", workspace) for a in spec]
        kind = backend_kind(backend, spec)
        level = reasoning_value(kind, params)
        if kind == "claude_cli" and level:                          # claude CLI: native --effort flag
            cmd += ["--effort", level]
        elif kind == "codex_cli" and level:                         # codex CLI: reasoning-effort config override
            cmd = cmd[:2] + ["-c", f"model_reasoning_effort={level}"] + cmd[2:]
        final_path = None
        if backend == "codex":
            fd, final_path = tempfile.mkstemp(prefix="jarvis-codex-final-", suffix=".txt")
            os.close(fd)
            cmd += ["--output-last-message", final_path]
        stdin = None
        if any("{prompt}" in a for a in cmd):
            cmd = [a.replace("{prompt}", prompt) for a in cmd]
        else:
            stdin = prompt
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "nonzero").strip()[:200])
        if final_path:
            try:
                final = Path(final_path).read_text().strip()
                if final:
                    return final
            finally:
                try:
                    os.unlink(final_path)
                except Exception:
                    pass
        return p.stdout.strip()

    def _invoke_http(self, spec, model, prompt, timeout, params=None):
        """HTTP chat backend: OpenAI-compatible by default (Ollama Cloud, OpenAI, OpenRouter, ...),
        or the Anthropic Messages API when spec.format == 'anthropic'."""
        params = params or {}
        kind = backend_kind("", spec)
        key = os.environ.get(spec.get("api_key_env", ""), "")
        if spec.get("format") == "anthropic":
            level = reasoning_value(kind, params)                  # thinking budget (control = thinking)
            payload = {"model": model, "max_tokens": 4096,
                       "messages": [{"role": "user", "content": prompt}]}
            if level in _THINK_BUDGET:                             # extended thinking (budget must be < max_tokens)
                budget = _THINK_BUDGET[level]
                payload["max_tokens"] = budget + 4096
                payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            body = json.dumps(payload).encode()
            headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
            if key:
                headers["x-api-key"] = key
            req = urllib.request.Request(spec["http"], data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
            text = "".join(p.get("text", "") for p in (data.get("content") or []) if isinstance(p, dict)).strip()
            if not text:
                raise RuntimeError("empty response from Anthropic backend")
            return text
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False}
        # OpenAI-style reasoning_effort. model_caps=["thinking"] forces the apply (the per-model caps gate
        # is a UI concern; ollama silently ignores reasoning_effort for non-thinking models — verified safe).
        level = reasoning_value(kind, params, model_caps=["thinking"])
        if level:
            payload["reasoning_effort"] = level
        nctx = context_num_ctx(kind, params)                  # ollama: run the model at the chosen context size
        if nctx:
            payload.setdefault("options", {})["num_ctx"] = nctx
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(spec["http"], data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("empty response from HTTP backend")
        return (choices[0].get("message", {}).get("content") or "").strip()

    def run_stream(self, role, prompt, on_delta, timeout=180, think=None, should_cancel=None, images=None,
                   tools=None):
        """Stream a reply, emitting on_delta(kind, text) as deltas arrive — kind is 'thinking' (the
        model's reasoning), 'text' (the answer), or 'tool' (a COMPLETE structured tool call as a JSON
        string {"name","arguments"} — OpenAI-style backends only). Returns the full answer text. Streams
        via the claude CLI (stream-json, with real thinking blocks) or an OpenAI-style HTTP SSE; backends
        that can't stream degrade to one on_delta('text', whole answer). `think` forces a thinking level;
        `should_cancel()` (if given) is polled to stop early (returns the partial). `tools` (OpenAI
        function specs) enables NATIVE structured tool-calling on HTTP backends — deterministic, instead
        of scraping tool calls out of free text."""
        params = (self.params or {}).get(role) or {}
        if think:
            params = {**params, "thinking": think}
        # Build the FULL target list like run() (role target + fallbacks) and advance through it on
        # failure — the old code only ever tried the role target / first fallback on the SAME backend,
        # so a *failing* (not merely absent) primary broke chat instead of failing over.
        targets, tried = [], []
        if self.routing.get(role):
            targets.append(self.routing[role])
        targets += [f for f in self.fallbacks if f not in targets]
        state = {"emitted": False}

        def emit(kind, text):                # once any token is shown, we must NOT restart on another
            state["emitted"] = True          # backend (that would duplicate the partial in the UI)
            on_delta(kind, text)

        for target in targets:
            if should_cancel and should_cancel():
                return ""
            backend, model = self._resolve(target)
            if not self._present(backend):
                tried.append(f"{backend}:absent"); continue
            spec = self.backends.get(backend)
            try:
                if backend == "claude" and not (isinstance(spec, dict) and spec.get("http")):
                    return self._stream_claude_cli(model, prompt, emit, timeout, params, should_cancel,
                                                   images, enable_mcp=bool(tools))
                if isinstance(spec, dict) and spec.get("http") and spec.get("format") != "anthropic":
                    return self._stream_http_openai(spec, model, prompt, emit, timeout, params, should_cancel, images, tools)
                out = self._invoke(backend, model, prompt, timeout, params)   # non-streamable -> one-shot
                emit("text", out)
                return out
            except Exception as e:
                if state["emitted"]:         # already streamed a partial -> surface, don't re-stream
                    raise
                tried.append(f"{backend}:{str(e)[:40]}")
                continue
        raise RuntimeError(f"no usable streaming backend for role '{role}' (tried: {tried})")

    def _claude_mcp_flags(self):
        """Give the `claude` CLI backend Jarvis's MCP servers via Claude Code's native --mcp-config
        (it can't use Jarvis's in-process tool registry). Returns (extra_args, cleanup_path). The temp
        config holds a live OAuth Bearer for HTTP servers, so it's written 0600 and deleted after use.
        --strict-mcp-config => only these servers (ignore the user's global ~/.claude config).
        --allowedTools mcp__<server> => pre-approve MCP tools so they run non-interactively (-p mode).
        Built-in tools (Read/Bash/...) are unaffected: allowedTools is additive, not restrictive."""
        try:
            from jarvis import mcp
            from jarvis.config import load
            conf, allowed = mcp.claude_cli_config(load())
        except Exception:
            return [], None
        if not conf.get("mcpServers"):
            return [], None
        fd, path = tempfile.mkstemp(prefix="jarvis-mcp-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(conf, f)                               # mkstemp is 0600 — bearer token stays private
        args = ["--mcp-config", path, "--strict-mcp-config"]
        if allowed:
            args += ["--allowedTools", ",".join(allowed)]
        return args, path

    def _stream_claude_cli(self, model, prompt, on_delta, timeout, params, should_cancel=None, images=None,
                           enable_mcp=False):
        import subprocess
        cmd = ["claude", "-p", "--model", model, "--output-format", "stream-json",
               "--include-partial-messages", "--verbose"]
        level = reasoning_value("claude_cli", params)        # native --effort (replaces the old keyword hack)
        if level:
            cmd += ["--effort", level]
        mcp_cleanup = None
        if enable_mcp:                                       # let the CLI natively use Jarvis's MCP servers
            mcp_args, mcp_cleanup = self._claude_mcp_flags()
            cmd += mcp_args
        if images:                                           # vision: send text + image blocks via stream-json input
            content = [{"type": "text", "text": prompt}]
            for path in images:
                b64, mt = _img_b64(path)
                content.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}})
            stdin_data = json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"
            cmd += ["--input-format", "stream-json"]
        else:
            stdin_data = prompt
        import threading
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
        p.stdin.write(stdin_data); p.stdin.close()
        full = ""
        # HARD wall-clock bound: `for line in p.stdout` blocks with no deadline, so a hung or very-slow
        # CLI (extended thinking that never yields a final block) would freeze the chat thread forever
        # and leave the message stuck streaming=True (the hanging cursor). A watchdog kills the process
        # at `timeout`, which EOFs stdout and ends the loop with whatever partial we already streamed.
        timed_out = {"v": False}
        def _kill():
            timed_out["v"] = True
            try: p.kill()
            except Exception: pass
        killer = threading.Timer(max(5, timeout), _kill)
        killer.daemon = True
        killer.start()
        try:
            for line in p.stdout:
                if should_cancel and should_cancel():        # interrupted -> stop now, keep partial
                    try: p.terminate()
                    except Exception: pass
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get("type") == "stream_event":
                    ev = ev.get("event", {})
                t = ev.get("type")
                if t == "content_block_delta":
                    d = ev.get("delta") or {}
                    if d.get("type") == "thinking_delta":
                        th = d.get("thinking") or ""
                        if th:
                            on_delta("thinking", th)
                    elif d.get("type") == "text_delta":
                        tx = d.get("text") or ""
                        if tx:
                            full += tx; on_delta("text", tx)
                elif t == "assistant" and not full:              # non-partial fallback
                    msg = ev.get("message", {})
                    txt = "".join(b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text")
                    if txt:
                        full = txt; on_delta("text", txt)
                elif t == "result" and not full:
                    r = ev.get("result") or ""
                    if r:
                        full = r; on_delta("text", r)
        finally:
            killer.cancel()
            if p.poll() is None:             # never leave an orphaned/stalled subprocess behind
                try:
                    p.kill(); p.wait(timeout=5)
                except Exception:
                    pass
            if mcp_cleanup:                  # remove the temp MCP config (it held a live bearer token)
                try:
                    os.unlink(mcp_cleanup)
                except Exception:
                    pass
        # A timeout with NO output is a real failure (fail over); a timeout WITH partial text keeps it.
        if not full and not (should_cancel and should_cancel()):
            raise RuntimeError("claude stream timed out with no text" if timed_out["v"]
                               else "claude stream produced no text")
        return full

    def _stream_http_openai(self, spec, model, prompt, on_delta, timeout, params, should_cancel=None,
                            images=None, tools=None):
        key = os.environ.get(spec.get("api_key_env", ""), "")
        if images:                                           # vision models: OpenAI-style image_url blocks
            content = [{"type": "text", "text": prompt}]
            for path in images:
                b64, mt = _img_b64(path)
                content.append({"type": "image_url", "image_url": {"url": f"data:{mt};base64,{b64}"}})
            messages = [{"role": "user", "content": content}]
        else:
            messages = [{"role": "user", "content": prompt}]
        payload = {"model": model, "messages": messages, "stream": True}
        if tools:                                            # NATIVE structured tool-calling (deterministic)
            payload["tools"] = tools
        # model_caps=["thinking"] forces the apply (per-model gate is a UI concern); ollama safely
        # ignores reasoning_effort for non-thinking models — verified in the research.
        _kind = backend_kind("", spec)
        level = reasoning_value(_kind, params, model_caps=["thinking"])
        if level:
            payload["reasoning_effort"] = level
        nctx = context_num_ctx(_kind, params)                # ollama: run the model at the chosen context size
        if nctx:
            payload.setdefault("options", {})["num_ctx"] = nctx
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(spec["http"], data=json.dumps(payload).encode(), headers=headers)
        full = ""
        tool_frags: dict = {}                                # index -> {"name","arguments"} (args stream in pieces)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                if should_cancel and should_cancel():
                    break
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    j = json.loads(data)
                except Exception:
                    continue
                delta = (j.get("choices") or [{}])[0].get("delta") or {}
                # some reasoning models stream a separate reasoning field
                rc = delta.get("reasoning_content") or delta.get("reasoning") or ""
                if rc:
                    on_delta("thinking", rc)
                d = delta.get("content") or ""
                if d:
                    full += d; on_delta("text", d)
                for tc in (delta.get("tool_calls") or []):   # assemble streamed function-call fragments
                    idx = tc.get("index", 0)
                    slot = tool_frags.setdefault(idx, {"name": "", "arguments": ""})
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
        for idx in sorted(tool_frags):                       # emit each COMPLETE structured tool call
            slot = tool_frags[idx]
            if slot.get("name"):
                on_delta("tool", json.dumps(slot))
        if not full and not tool_frags and not (should_cancel and should_cancel()):
            raise RuntimeError("http stream produced no text")
        return full

    def turn(self, role, prompt, *, tools=None, timeout=200, think=None,
             should_cancel=None, images=None) -> dict:
        """Provider-agnostic Brain.turn() — accumulates a full reply into
        {text, thinking, tool_calls} without streaming to the UI.

        HTTP backends return native structured tool_calls; CLI backends return text (text-recovery
        in the caller). Use run_stream() when per-token SSE push is needed; use turn() for the
        autonomous tick and any non-interactive tool loop."""
        text_parts: list = []
        thinking_parts: list = []
        tool_calls: list = []  # raw JSON strings {"name": ..., "arguments": ...}

        def on_delta(kind: str, d: str) -> None:
            if kind == "text":
                text_parts.append(d)
            elif kind == "thinking":
                thinking_parts.append(d)
            elif kind == "tool":
                tool_calls.append(d)

        try:
            self.run_stream(role, prompt, on_delta, timeout=timeout, think=think,
                            should_cancel=should_cancel, images=images, tools=tools)
        except Exception:
            # non-streaming fallback for CLI backends that can't stream
            try:
                out = self.run(role, prompt, timeout=timeout)
                text_parts.append(out)
            except Exception:
                pass

        return {
            "text": "".join(text_parts).strip(),
            "thinking": "".join(thinking_parts),
            "tool_calls": tool_calls,
        }

    def run(self, role, prompt, timeout=120):
        params = (self.params or {}).get(role) or {}
        targets, tried = [], []
        if self.routing.get(role):
            targets.append(self.routing[role])
        targets += [f for f in self.fallbacks if f not in targets]
        for target in targets:
            backend, model = self._resolve(target)
            if not self._present(backend):
                tried.append(f"{backend}:absent"); continue
            try:
                return self._invoke(backend, model, prompt, timeout, params)
            except Exception as e:
                tried.append(f"{backend}:{str(e)[:50]}")
        raise RuntimeError(f"no usable LLM backend for role '{role}' (tried: {tried})")

def build_llm(cfg):
    llm = cfg.get("llm") or {}
    if not llm:
        return None
    return RoutingLLM(llm.get("routing"), llm.get("backends"),
                      llm.get("aliases"), llm.get("fallbacks"), llm.get("params"))
