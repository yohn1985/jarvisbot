"""Jarvis <-> owner messages, surfaced in the dashboard chat. Append-only JSONL.

Jarvis posts notes and QUESTIONS (things it can't figure out alone); the owner answers in
the dashboard; the answer flows back into the next tick as input. Stdlib only.
"""
from __future__ import annotations
import json, time, uuid
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / "state"
MSGS = STATE / "messages.jsonl"


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _append(row: dict) -> dict:
    STATE.mkdir(exist_ok=True)
    with open(MSGS, "a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def _all() -> list[dict]:
    if not MSGS.exists():
        return []
    out = []
    for line in MSGS.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def post_question(text: str, ref: str = "") -> dict:
    return _append({"id": uuid.uuid4().hex[:8], "ts": _now(), "from": "jarvis",
                    "kind": "question", "text": text, "ref": ref,
                    "answered": False, "answer": None})


def post_note(text: str, ref: str = "") -> dict:
    return _append({"id": uuid.uuid4().hex[:8], "ts": _now(), "from": "jarvis",
                    "kind": "note", "text": text, "ref": ref})


def recent(limit: int = 100) -> list[dict]:
    return _all()[-limit:]


def unanswered() -> list[dict]:
    return [m for m in _all() if m.get("kind") == "question" and not m.get("answered")]


def answer(qid: str, text: str) -> bool:
    rows, found = _all(), False
    for m in rows:
        if m.get("id") == qid and m.get("kind") == "question":
            m["answered"], m["answer"] = True, text
            found = True
    if found:
        with open(MSGS, "w") as f:
            for m in rows:
                f.write(json.dumps(m) + "\n")
        _append({"id": uuid.uuid4().hex[:8], "ts": _now(), "from": "owner",
                 "kind": "answer", "text": text, "ref": qid})
    return found


def say(text: str) -> dict:
    """Owner-initiated message to Jarvis (picked up on the next tick as input)."""
    return _append({"id": uuid.uuid4().hex[:8], "ts": _now(), "from": "owner",
                    "kind": "message", "text": text, "ref": ""})
