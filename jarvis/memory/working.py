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

    def lock(self, name: str, ttl: int = 300):
        """Mutual exclusion with an OWNER TOKEN. Returns the token if acquired, else None.
        The token is required to unlock, so a tick that outlives its TTL (key expires, another
        acquirer takes it) can NOT delete the new holder's lock on its way out.
        NOTE: the in-memory fallback is process-local only — it does NOT exclude across processes;
        run a single loop, or use Redis, for a real system-wide mutex."""
        import uuid
        token = uuid.uuid4().hex
        if self.r:
            return token if self.r.set(f"lock:{name}", token, nx=True, ex=ttl) else None
        if self._mem.get(f"lock:{name}"):
            return None
        self._mem[f"lock:{name}"] = token
        return token

    def unlock(self, name: str, token: str | None = None) -> None:
        """Release only if we still own it (compare-and-delete)."""
        if self.r:
            try:
                self.r.eval("if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end",
                            1, f"lock:{name}", token or "")
            except Exception:
                if self.r.get(f"lock:{name}") == token:
                    self.r.delete(f"lock:{name}")
        elif token is None or self._mem.get(f"lock:{name}") == token:
            self._mem.pop(f"lock:{name}", None)


def build_working(cfg: dict) -> WorkingMemory:
    return WorkingMemory(cfg)
