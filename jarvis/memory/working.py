"""Working memory (M-redis) — the fast, ephemeral tier.

Redis-backed: the wake queue, cross-tick/worker LOCKS (so two ticks or two workers don't
collide — the swarm's stigmergy coordination), pheromone signals, and current-tick scratch.
Degrades to an in-process dict when Redis is absent, so Jarvis always works. Stdlib + redis.
"""
from __future__ import annotations
import os, re


def _expand(s: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), s or "")


class WorkingMemory:
    def __init__(self, cfg: dict):
        self.r = None
        self._mem: dict = {}
        url = _expand((cfg.get("memory", {}).get("working", {}) or {}).get("url", ""))
        if url:
            try:
                import redis
                self.r = redis.from_url(url, socket_connect_timeout=3, decode_responses=True)
                self.r.ping()
            except Exception:
                self.r = None

    @property
    def backend(self) -> str:
        return "redis" if self.r else "memory"

    def set(self, key: str, val: str, ttl: int | None = None) -> None:
        if self.r:
            self.r.set(key, val, ex=ttl)
        else:
            self._mem[key] = val

    def get(self, key: str):
        return self.r.get(key) if self.r else self._mem.get(key)

    def push(self, queue: str, val: str) -> None:
        if self.r:
            self.r.rpush(queue, val)
        else:
            self._mem.setdefault(queue, []).append(val)

    def pop(self, queue: str):
        if self.r:
            return self.r.lpop(queue)
        q = self._mem.get(queue, [])
        return q.pop(0) if q else None

    def lock(self, name: str, ttl: int = 300) -> bool:
        """Best-effort mutual exclusion (two ticks/workers must not claim the same thing)."""
        if self.r:
            return bool(self.r.set(f"lock:{name}", "1", nx=True, ex=ttl))
        if self._mem.get(f"lock:{name}"):
            return False
        self._mem[f"lock:{name}"] = "1"
        return True

    def unlock(self, name: str) -> None:
        if self.r:
            self.r.delete(f"lock:{name}")
        else:
            self._mem.pop(f"lock:{name}", None)


def build_working(cfg: dict) -> WorkingMemory:
    return WorkingMemory(cfg)
