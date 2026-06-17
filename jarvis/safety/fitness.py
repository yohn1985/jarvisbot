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
    """The decision pipeline still produces a valid rung. SHADOW only: perceive+decide, never act() —
    a full kernel.tick() inside the gate could launch a ~300s network scan / LLM calls."""
    code = ("import sys; sys.path.insert(0, '.');"
            "from jarvis import kernel; from jarvis.config import load;"
            "c = load(); w = kernel.perceive(c); rung, action = kernel.decide(c, w);"
            "print('RUNG=' + str(rung))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(ROOT), env={"PATH": "/usr/bin:/bin", "JARVIS_NO_THINK": "1"})
    return ("kernel_ticks", "RUNG=p" in r.stdout, (r.stderr or r.stdout).strip()[-160:])


def c_verify_failclosed():
    """Guards the keystone: verify() must FAIL CLOSED — with no brain it must NOT return survives."""
    code = ("import sys; sys.path.insert(0, '.');"
            "from jarvis.verify import verify; v = verify({}, 'x');"
            "print('VFC=' + ('ok' if (v.get('survives') is False and v.get('verified') is False) else 'FAIL'))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    return ("verify_fail_closed", "VFC=ok" in r.stdout, (r.stderr or r.stdout).strip()[-160:])


def c_router_builds():
    code = ("import sys; sys.path.insert(0, '.');"
            "from jarvis.config import load; from jarvis.adapters.llm import build_llm;"
            "print('LLM_OK' if build_llm(load()) else 'LLM_NONE')")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    return ("router_builds", "LLM_OK" in r.stdout, (r.stderr or "").strip()[-160:])


def c_memory_reads():
    """REAL round-trip: write an episode then recall it (the old check passed even on dead memory).
    Uses a throwaway JARVIS_LEDGER so it never pollutes the live ledger."""
    code = ("import sys, os, tempfile; sys.path.insert(0, '.');"
            "os.environ['JARVIS_LEDGER'] = tempfile.mktemp(suffix='.jsonl');"
            "from jarvis.config import load; from jarvis.memory.store import build_store;"
            "s = build_store(load());"
            "s.remember(sig='_fit_rt', area='_fit', label='fit', symptom='rt', outcome='ok');"
            "hit = any(r.get('sig') == '_fit_rt' for r in s.recall(area='_fit', limit=20));"
            "print('MEMRT=' + ('ok:' + s.backend if hit else 'FAIL'))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    return ("memory_roundtrip", "MEMRT=ok" in r.stdout, (r.stderr or r.stdout).strip()[-160:])


def c_working_mem():
    code = ("import sys; sys.path.insert(0, '.');"
            "from jarvis.config import load; from jarvis.memory.working import build_working;"
            "w = build_working(load()); w.set('_fit', '1'); print('WM=' + w.backend + ':' + str(w.get('_fit')))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    return ("working_mem", "WM=" in r.stdout and ":1" in r.stdout, (r.stderr or "").strip()[-160:])


def c_unit_tests():
    """Run the stdlib unittest suite. This is the regression gate: a self-edit (or any change) that
    breaks observable behavior fails here, so the seatbelt won't adopt it. Degrades gracefully — a
    missing suite isn't penalized, but a genuine failure (or a hang) counts as not-ok."""
    tests_dir = ROOT / "tests"
    if not tests_dir.exists():
        return ("unit_tests", True, "no tests dir")
    try:
        r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
                           capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    except Exception as e:
        return ("unit_tests", False, str(e)[:160])
    tail = (r.stderr or r.stdout).strip().splitlines()
    return ("unit_tests", r.returncode == 0, (tail[-1] if tail else "")[:160])


def main():
    checks = [c_syntax(), c_kernel_ticks(), c_verify_failclosed(), c_router_builds(),
              c_memory_reads(), c_working_mem(), c_unit_tests()]
    score = sum(1 for _, ok, _ in checks if ok)
    print(json.dumps({"score": score, "max": len(checks),
                      "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks]}))


if __name__ == "__main__":
    main()
