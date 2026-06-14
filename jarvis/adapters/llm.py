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

class RoutingLLM:
    def __init__(self, routing, backends, aliases, fallbacks):
        self.routing = routing or {}
        self.backends = backends or DEFAULT_BACKENDS
        self.aliases = aliases or {}
        self.fallbacks = fallbacks or []

    def _resolve(self, target):
        backend, _, model = (target or "").partition(":")
        return backend, self.aliases.get(model, model)

    def _present(self, backend):
        spec = self.backends.get(backend)
        if isinstance(spec, dict) and spec.get("http"):       # HTTP backend: present if its key is set
            env = spec.get("api_key_env")
            return bool(spec["http"]) and (not env or bool(os.environ.get(env)))
        return bool(spec) and shutil.which(spec[0]) is not None

    def _invoke(self, backend, model, prompt, timeout):
        spec = self.backends[backend]
        if isinstance(spec, dict) and spec.get("http"):
            return self._invoke_http(spec, model, prompt, timeout)
        cmd = [a.replace("{model}", model) for a in spec]
        stdin = None
        if any("{prompt}" in a for a in cmd):
            cmd = [a.replace("{prompt}", prompt) for a in cmd]
        else:
            stdin = prompt
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "nonzero").strip()[:200])
        return p.stdout.strip()

    def _invoke_http(self, spec, model, prompt, timeout):
        """HTTP chat backend: OpenAI-compatible by default (Ollama Cloud, OpenAI, OpenRouter, ...),
        or the Anthropic Messages API when spec.format == 'anthropic'."""
        key = os.environ.get(spec.get("api_key_env", ""), "")
        if spec.get("format") == "anthropic":
            body = json.dumps({"model": model, "max_tokens": 1024,
                               "messages": [{"role": "user", "content": prompt}]}).encode()
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
        body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                           "stream": False}).encode()
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

    def run(self, role, prompt, timeout=120):
        targets, tried = [], []
        if self.routing.get(role):
            targets.append(self.routing[role])
        targets += [f for f in self.fallbacks if f not in targets]
        for target in targets:
            backend, model = self._resolve(target)
            if not self._present(backend):
                tried.append(f"{backend}:absent"); continue
            try:
                return self._invoke(backend, model, prompt, timeout)
            except Exception as e:
                tried.append(f"{backend}:{str(e)[:50]}")
        raise RuntimeError(f"no usable LLM backend for role '{role}' (tried: {tried})")

def build_llm(cfg):
    llm = cfg.get("llm") or {}
    if not llm:
        return None
    return RoutingLLM(llm.get("routing"), llm.get("backends"),
                      llm.get("aliases"), llm.get("fallbacks"))
