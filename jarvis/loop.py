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
    # Telegram channel (no-op without a token): instantiated once so getUpdates offset persists.
    tg = None
    try:
        from jarvis.adapters.notifier import TelegramNotifier
        tg = TelegramNotifier()
    except Exception:
        tg = None
    from jarvis.memory.working import build_working
    wm = build_working(load())              # working memory (redis) for the cross-tick lock
    print(f"[jarvis] working memory: {wm.backend}")
    while not stop["v"]:
        cfg = load()                        # live config reload each wake
        if tg and tg.enabled:               # ingest owner replies from Telegram into the chat
            try:
                from jarvis import messaging
                for txt in tg.poll():
                    messaging.say(txt, conv="telegram", title="Telegram")
            except Exception:
                pass
        if not wm.lock("tick", ttl=600):    # don't let two loops/ticks collide (stigmergy)
            time.sleep(5)
            continue
        try:
            decision = kernel.tick(cfg)
        finally:
            wm.unlock("tick")
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
