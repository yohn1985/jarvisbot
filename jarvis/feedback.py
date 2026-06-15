"""Feedback / experience memory — the substrate for self-improvement.

Records what Jarvis did and how it turned out, so over time it can recall what works. This is the
'reflect' step the loop was missing: every tick writes an experience here. Uses the episodic store
(Postgres) when available AND always appends to state/feedback.jsonl so it works with no database.

Stdlib only.
"""
from __future__ import annotations
import json, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "state" / "feedback.jsonl"


def record(cfg, *, kind, area="", summary="", outcome="", detail=""):
    """Record one experience (an action + how it went)."""
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, "area": area,
           "summary": summary, "outcome": outcome, "detail": (detail or "")[:1000]}
    try:                                  # causal long-term memory: persist on EITHER backend so the
        from jarvis.memory.store import build_store   # reflect step actually reaches recall (perceive/think)
        build_store(cfg).remember(
            sig=f"{kind}:{area}:{(summary or '')[:40]}", area=area, source="jarvis",
            label=kind, symptom=summary, outcome=outcome, resolution=(detail or "")[:400])
    except Exception:
        pass
    try:                                  # always-on local trail (works with no DB)
        LEDGER.parent.mkdir(exist_ok=True)
        with open(LEDGER, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass
    return row


def recent(limit=15):
    if not LEDGER.exists():
        return []
    rows = []
    for line in LEDGER.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows[-limit:]
