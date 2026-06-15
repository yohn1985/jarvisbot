"""Spawn a bounded sub-agent to do a piece of work — Jarvis's hands beyond a single thought.

Generic (this borrows the ai-exec *pattern*, not its env-specific code): a `role` selects the model
via the LLM router, so spawned agents run on the cheap/agent tier (e.g. DeepSeek/Ollama) while the
main brain stays frontier. Every spawn's outcome is recorded to feedback (experience memory), so
Jarvis can learn what works.

    spawn(cfg, "summarize these logs ...", role="researcher")
"""
from __future__ import annotations
import time


def spawn(cfg, task, role="researcher", timeout=180):
    """Run a bounded agent task on the tier `role` maps to; capture the result; record feedback."""
    from jarvis.adapters.llm import build_llm
    llm = build_llm(cfg)
    model = ((cfg.get("llm", {}) or {}).get("routing", {}) or {}).get(role, "")
    if not llm:
        return {"ok": False, "role": role, "error": "no brain configured"}
    t0 = time.time()
    try:
        out = llm.run(role, task, timeout=timeout)
        res = {"ok": True, "role": role, "model": model, "output": out, "secs": round(time.time() - t0, 1)}
    except Exception as e:
        res = {"ok": False, "role": role, "model": model, "error": str(e)[:200], "secs": round(time.time() - t0, 1)}
    try:
        from jarvis import feedback
        feedback.record(cfg, kind="spawn", area=role, summary=str(task)[:120],
                        outcome="ok" if res["ok"] else "fail",
                        detail=(res.get("output") or res.get("error") or "")[:600])
    except Exception:
        pass
    return res
