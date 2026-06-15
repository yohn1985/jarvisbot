"""LLM provider router (the mind): role -> "backend:model", dispatched to a CLI backend.
Generalizes ai-exec. Backends are CLI command templates in config ({model}/{prompt}; no
{prompt} placeholder => prompt is piped on stdin). A routed backend that's absent or fails
falls through to `fallbacks`. Stdlib only."""
from __future__ import annotations
import json, os, shutil, subprocess, urllib.request

DEFAULT_BACKENDS = {
    "claude": ["claude", "-p", "--model", "{model}"],
    "codex":  ["codex", "exec", "--model", "{model}", "{prompt}"],
    "ollama": ["ollama", "run", "{model}", "{prompt}"],
}

# A backend may also be an HTTP (OpenAI-compatible) endpoint instead of a CLI list, e.g.:
#   ollama_cloud: {http: "https://ollama.com/v1/chat/completions", api_key_env: OLLAMA_API_KEY}
# This covers Ollama Cloud and any OpenAI-compatible API (subscription key, no local CLI needed).

# Reasoning controls, applied per role from llm.params (set in the dashboard). Only models that
# support them are offered the dropdowns; here we translate the chosen level to each backend's API.
_THINK_BUDGET = {"low": 4000, "medium": 10000, "high": 24000, "max": 32000}   # Anthropic thinking budget_tokens
_THINK_KEYWORD = {"low": "\n\nThink about this carefully.",          # claude CLI honors think/ultrathink
                  "medium": "\n\nThink hard about this.",
                  "high": "\n\nUltrathink about this.",
                  "max": "\n\nUltrathink as hard as you possibly can about this."}


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
        cmd = [a.replace("{model}", model) for a in spec]
        effort, thinking = params.get("effort"), params.get("thinking")
        if backend == "claude" and thinking in _THINK_KEYWORD:      # claude CLI: extended thinking via keyword
            prompt = prompt + _THINK_KEYWORD[thinking]
        if backend == "codex" and effort and effort != "default":   # codex CLI: reasoning-effort config override
            cmd = cmd[:2] + ["-c", f"model_reasoning_effort={effort}"] + cmd[2:]
        stdin = None
        if any("{prompt}" in a for a in cmd):
            cmd = [a.replace("{prompt}", prompt) for a in cmd]
        else:
            stdin = prompt
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "nonzero").strip()[:200])
        return p.stdout.strip()

    def _invoke_http(self, spec, model, prompt, timeout, params=None):
        """HTTP chat backend: OpenAI-compatible by default (Ollama Cloud, OpenAI, OpenRouter, ...),
        or the Anthropic Messages API when spec.format == 'anthropic'."""
        params = params or {}
        effort, thinking = params.get("effort"), params.get("thinking")
        key = os.environ.get(spec.get("api_key_env", ""), "")
        if spec.get("format") == "anthropic":
            payload = {"model": model, "max_tokens": 4096,
                       "messages": [{"role": "user", "content": prompt}]}
            if thinking in _THINK_BUDGET:                          # extended thinking (budget must be < max_tokens)
                budget = _THINK_BUDGET[thinking]
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
        if effort and effort != "default":                        # OpenAI / o-series / gpt-5 reasoning effort
            payload["reasoning_effort"] = effort
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

    def run_stream(self, role, prompt, on_delta, timeout=180, think=None, should_cancel=None, images=None):
        """Stream a reply, emitting on_delta(kind, text) as deltas arrive — kind is 'thinking' (the
        model's reasoning) or 'text' (the answer). Returns the full answer text. Streams via the claude
        CLI (stream-json, with real thinking blocks) or an OpenAI-style HTTP SSE; backends that can't
        stream degrade to one on_delta('text', whole answer). `think` forces a thinking level;
        `should_cancel()` (if given) is polled to stop early (returns the partial)."""
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
                    return self._stream_claude_cli(model, prompt, emit, timeout, params, should_cancel, images)
                if isinstance(spec, dict) and spec.get("http") and spec.get("format") != "anthropic":
                    return self._stream_http_openai(spec, model, prompt, emit, timeout, params, should_cancel, images)
                out = self._invoke(backend, model, prompt, timeout, params)   # non-streamable -> one-shot
                emit("text", out)
                return out
            except Exception as e:
                if state["emitted"]:         # already streamed a partial -> surface, don't re-stream
                    raise
                tried.append(f"{backend}:{str(e)[:40]}")
                continue
        raise RuntimeError(f"no usable streaming backend for role '{role}' (tried: {tried})")

    def _stream_claude_cli(self, model, prompt, on_delta, timeout, params, should_cancel=None, images=None):
        import subprocess
        thinking = params.get("thinking")
        if thinking in _THINK_KEYWORD:                       # ask Opus to actually think (visible block)
            prompt = prompt + _THINK_KEYWORD[thinking]
        cmd = ["claude", "-p", "--model", model, "--output-format", "stream-json",
               "--include-partial-messages", "--verbose"]
        if images:                                           # vision: send text + image blocks via stream-json input
            content = [{"type": "text", "text": prompt}]
            for path in images:
                b64, mt = _img_b64(path)
                content.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}})
            stdin_data = json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"
            cmd += ["--input-format", "stream-json"]
        else:
            stdin_data = prompt
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
        p.stdin.write(stdin_data); p.stdin.close()
        full = ""
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
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        finally:
            if p.poll() is None:             # never leave an orphaned/stalled subprocess behind
                try:
                    p.kill(); p.wait(timeout=5)
                except Exception:
                    pass
        if not full and not (should_cancel and should_cancel()):
            raise RuntimeError("claude stream produced no text")
        return full

    def _stream_http_openai(self, spec, model, prompt, on_delta, timeout, params, should_cancel=None, images=None):
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
        effort = params.get("effort")
        if effort and effort != "default":
            payload["reasoning_effort"] = effort
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(spec["http"], data=json.dumps(payload).encode(), headers=headers)
        full = ""
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
        if not full and not (should_cancel and should_cancel()):
            raise RuntimeError("http stream produced no text")
        return full

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
