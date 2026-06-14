#!/usr/bin/env python3
"""Tamper-proof fitness harness (M6).

Deterministic checks the MIND CANNOT score itself on — this runs as a SEPARATE process and
prints a single JSON line {"score": N, "max": M, "checks": [...]}. Higher is better. The
self-modification seatbelt uses this as the empirical fitness gate (Darwin-Gödel-Machine style):
a self-edit is only adopted if it does not LOWER the score (and survives an independent red-team).

Add eval cases here as Jarvis grows — this is the benchmark a self-edit must beat. Seeded with
the A/B and rejected-PR lessons from the loopback (the "history of what failed", DGM-style).
"""
from __future__ import annotations
import ast, json, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def c_syntax():
    bad = []
    for p in (ROOT / "jarvis").rglob("*.py"):
        try:
            ast.parse(p.read_text())
        except Exception as e:
            bad.append(f"{p.name}: {e}")
    return ("syntax_valid", not bad, "; ".join(bad)[:160])


def c_kernel_ticks():
    """The kernel still produces a valid decision (LLM disabled for determinism)."""
    code = ("import sys; sys.path.insert(0, '.');"
            "from jarvis import kernel; from jarvis.config import load;"
            "c = load(); c.setdefault('llm', {})['think_on_tick'] = False;"
            "d = kernel.tick(c); print('RUNG=' + str(d.get('rung')))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(ROOT), env={"PATH": "/usr/bin:/bin", "JARVIS_NO_THINK": "1"})
    return ("kernel_ticks", "RUNG=p" in r.stdout, (r.stderr or r.stdout).strip()[-160:])


def c_router_builds():
    code = ("import sys; sys.path.insert(0, '.');"
            "from jarvis.config import load; from jarvis.adapters.llm import build_llm;"
            "print('LLM_OK' if build_llm(load()) else 'LLM_NONE')")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    return ("router_builds", "LLM_OK" in r.stdout, (r.stderr or "").strip()[-160:])


def c_memory_reads():
    code = ("import sys; sys.path.insert(0, '.');"
            "from jarvis.config import load; from jarvis.memory.store import build_store;"
            "s = build_store(load()); print('MEM=' + s.backend + ':' + str(len(s.recent(3))))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    return ("memory_reads", "MEM=" in r.stdout, (r.stderr or "").strip()[-160:])


def main():
    checks = [c_syntax(), c_kernel_ticks(), c_router_builds(), c_memory_reads()]
    score = sum(1 for _, ok, _ in checks if ok)
    print(json.dumps({"score": score, "max": len(checks),
                      "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks]}))


if __name__ == "__main__":
    main()
