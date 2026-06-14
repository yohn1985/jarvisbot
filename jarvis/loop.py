#!/usr/bin/env python3
"""Persistent wake loop (M5) — the heartbeat that keeps Jarvis alive.

Runs a kernel tick, then sleeps until the soonest of: its self-scheduled next wake, the
heartbeat floor, or an alert wake-marker (state/wake — touched by /api/alert or an external
Alertmanager webhook). Reloads config every wake so changes take effect live. SIGINT/SIGTERM
stop it cleanly. `--once` runs a single tick and exits.
"""
from __future__ import annotations
import signal, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
STATE = ROOT / "state"
WAKE = STATE / "wake"          # touch to wake immediately (alert webhook / owner)

from jarvis.config import load
from jarvis import kernel


def next_delay(cfg: dict, decision: dict) -> int:
    """Self-scheduling: soon if there's live work, the heartbeat floor if idle."""
    hb = int((cfg.get("wake", {}) or {}).get("heartbeat_seconds", 1800))
    if decision.get("asked"):
        return min(hb, 120)                 # waiting on the owner — check back soon-ish
    rung = decision.get("rung", "")
    if rung in ("p0_active_incident", "p1_unfinished_wip", "p2_self_caused_regression"):
        return 60
    if rung == "p3_needs_human_backlog":
        return 120
    return hb                               # p4/p5 — idle cadence


def run(once: bool = False):
    STATE.mkdir(exist_ok=True)
    stop = {"v": False}
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *a: stop.__setitem__("v", True))
    print(f"[jarvis] wake loop up (heartbeat floor {load().get('wake',{}).get('heartbeat_seconds',1800)}s; "
          f"touch {WAKE} to wake)")
    while not stop["v"]:
        cfg = load()                        # live config reload each wake
        decision = kernel.tick(cfg)
        delay = next_delay(cfg, decision)
        print(f"[jarvis] tick {decision['ts']} rung={decision['rung']} -> next wake in {delay}s")
        if once:
            return decision
        waited = 0
        while waited < delay and not stop["v"]:
            if WAKE.exists():
                WAKE.unlink(missing_ok=True)
                print("[jarvis] woken by alert marker")
                break
            time.sleep(1)
            waited += 1
    print("[jarvis] wake loop stopped")


if __name__ == "__main__":
    run(once="--once" in sys.argv)
