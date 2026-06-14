"""LLM provider router (the mind): role -> "backend:model", dispatched to a CLI backend.
Generalizes ai-exec. Backends are CLI command templates in config ({model}/{prompt}; no
{prompt} placeholder => prompt is piped on stdin). A routed backend that's absent or fails
falls through to `fallbacks`. Stdlib only."""
from __future__ import annotations
import shutil, subprocess

DEFAULT_BACKENDS = {
    "claude": ["claude", "-p", "--model", "{model}"],
    "codex":  ["codex", "exec", "--model", "{model}", "{prompt}"],
    "ollama": ["ollama", "run", "{model}", "{prompt}"],
}

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
        cmd = self.backends.get(backend)
        return bool(cmd) and shutil.which(cmd[0]) is not None

    def _invoke(self, backend, model, prompt, timeout):
        cmd = [a.replace("{model}", model) for a in self.backends[backend]]
        stdin = None
        if any("{prompt}" in a for a in cmd):
            cmd = [a.replace("{prompt}", prompt) for a in cmd]
        else:
            stdin = prompt
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "nonzero").strip()[:200])
        return p.stdout.strip()

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
