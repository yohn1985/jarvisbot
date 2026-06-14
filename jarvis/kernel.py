#!/usr/bin/env python3
"""
The kernel: ONE bounded tick. The kernel is dumb and reliable; the intelligence
is the (eventual) single 'mind' call per tick. This stub runs in SHADOW mode on
the stdlib alone, so `./install.sh breathe` works before anything is installed.

    wake -> perceive -> orient -> decide(priority ladder) -> act -> reflect
"""
from __future__ import annotations
import sys, datetime
sys.path.insert(0, __file__.rsplit("/jarvis/", 1)[0])
from jarvis.config import load

def perceive(cfg) -> dict:
    # Real perception lives in jarvis/perceive.py (bounded, source-degrades-safe).
    # Falls back to an empty world if that module isn't present yet.
    try:
        from jarvis.perceive import perceive as _real
        return _real(cfg)
    except Exception:
        return {"active_incident": None, "unfinished_wip": None,
                "self_caused_regression": None, "needs_human_backlog": [],
                "stalest_area": "telephony/whatsapp"}

def orient(cfg, world) -> str:
    # Rebuild "who am I" from memory. Stub: identity from config.
    return f"I am {cfg['identity']['name']} ({cfg['identity'].get('mode','shadow')} mode)."

def decide(cfg, world) -> tuple[str, str]:
    """Pick the highest non-empty rung of the priority ladder -> (rung, action)."""
    ladder = {
        "p0_active_incident":        world.get("active_incident"),
        "p1_unfinished_wip":         world.get("unfinished_wip"),
        "p2_self_caused_regression": world.get("self_caused_regression"),
        "p3_needs_human_backlog":    world.get("needs_human_backlog") or None,
        "p4_self_maintenance":       None,
        "p5_curiosity":              world.get("stalest_area"),
    }
    for rung in cfg["priorities"]:
        if ladder.get(rung):
            return rung, _action_for(rung, ladder[rung])
    return "p5_curiosity", "explore an unknown area"

def _action_for(rung, payload):
    return {
        "p0_active_incident":        f"resolve incident: {payload}",
        "p1_unfinished_wip":         f"continue WIP: {payload}",
        "p2_self_caused_regression": f"fix my own regression: {payload}",
        "p3_needs_human_backlog":    f"take one needs-human issue and run deep-fix",
        "p5_curiosity":              f"explore stalest area: {payload}",
    }.get(rung, str(payload))

def tick(cfg) -> dict:
    world = perceive(cfg)
    who = orient(cfg, world)
    rung, action = decide(cfg, world)
    mode = cfg["identity"].get("mode", "shadow")
    decision = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "who": who, "rung": rung, "action": action, "mode": mode,
                "would_execute": mode != "shadow",
                "backlog": world.get("_backlog_count", 0)}
    # record the tick so the dashboard can show it (best-effort)
    try:
        from jarvis.runtime import record
        record(mode=f"tick/{rung}", target=action[:60], pool="kernel",
               status="would" if mode == "shadow" else "ok")
    except Exception:
        pass
    return decision

def main():
    cfg = load()
    d = tick(cfg)
    print(f"\n  \033[36m●\033[0m {d['who']}")
    print(f"    tick {d['ts']}")
    print(f"    decided rung : {d['rung']}")
    print(f"    action       : {d['action']}")
    verb = "WOULD (shadow — not executing)" if not d["would_execute"] else "EXECUTING"
    print(f"    {verb}\n")

if __name__ == "__main__":
    main()
