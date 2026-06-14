"""LLM provider adapter = a router over a model catalog (claude/codex/ollama).
Generalizes ai-exec: role -> model, with cost/capability tiering + fallbacks."""
from abc import ABC, abstractmethod

class LLM(ABC):
    @abstractmethod
    def run(self, role: str, prompt: str, **kw) -> str: ...

class RoutingLLM(LLM):
    def __init__(self, routing: dict, backends: dict):
        self.routing, self.backends = routing, backends
    def run(self, role: str, prompt: str, **kw) -> str:
        target = self.routing.get(role) or self.routing.get("triage")
        # TODO: dispatch target ("backend:model") to the right CLI backend.
        raise NotImplementedError(f"route {role} -> {target}")
