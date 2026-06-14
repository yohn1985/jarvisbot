"""Jarvis <-> owner messages, threaded into CONVERSATIONS (Gemini-style sidebar).

Append-only JSONL. Each message carries a `conv` (conversation id) + `conv_title`. Jarvis
threads its questions by topic (slug of the ref), so repeated questions about the same thing
land in one conversation. The owner can start a New chat. Stdlib only.
"""
from __future__ import annotations
import json, re, time, uuid
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / "state"
MSGS = STATE / "messages.jsonl"


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:48]
    return s or uuid.uuid4().hex[:8]


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


def _conv_of(m: dict) -> str:
    return m.get("conv") or "general"


def conversations() -> list[dict]:
    """One entry per thread: id, title, last_ts, message count, open-question count."""
    convs: dict[str, dict] = {}
    for m in _all():
        c = _conv_of(m)
        e = convs.setdefault(c, {"id": c, "title": c, "last_ts": m["ts"], "count": 0, "open_q": 0})
        e["last_ts"] = m["ts"]
        e["count"] += 1
        if m.get("conv_title"):
            e["title"] = m["conv_title"]
        if m.get("kind") == "question" and not m.get("answered"):
            e["open_q"] += 1
    return sorted(convs.values(), key=lambda c: c["last_ts"], reverse=True)


def messages(conv: str | None = None, limit: int = 200) -> list[dict]:
    rows = _all()
    if conv:
        rows = [m for m in rows if _conv_of(m) == conv]
    return rows[-limit:]


def recent(limit: int = 100) -> list[dict]:
    return _all()[-limit:]


def open_questions() -> int:
    return sum(1 for m in _all() if m.get("kind") == "question" and not m.get("answered"))


def post_question(text: str, ref: str = "", conv: str | None = None, title: str | None = None) -> dict:
    conv = conv or _slug(ref) or uuid.uuid4().hex[:8]
    title = title or (ref[:48] if ref else text[:48])
    return _append({"id": uuid.uuid4().hex[:8], "ts": _now(), "from": "jarvis",
                    "conv": conv, "conv_title": title, "kind": "question",
                    "text": text, "ref": ref, "answered": False, "answer": None})


def post_note(text: str, ref: str = "", conv: str | None = None, title: str | None = None) -> dict:
    conv = conv or _slug(ref) or "general"
    return _append({"id": uuid.uuid4().hex[:8], "ts": _now(), "from": "jarvis",
                    "conv": conv, "conv_title": title or (ref[:48] if ref else "note"),
                    "kind": "note", "text": text, "ref": ref})


def say(text: str, conv: str = "general", title: str | None = None) -> dict:
    return _append({"id": uuid.uuid4().hex[:8], "ts": _now(), "from": "owner",
                    "conv": conv, "conv_title": title or (text[:48] if conv != "general" else None),
                    "kind": "message", "text": text, "ref": ""})


def answer(qid: str, text: str) -> bool:
    rows, found, conv = _all(), False, "general"
    for m in rows:
        if m.get("id") == qid and m.get("kind") == "question":
            m["answered"], m["answer"] = True, text
            conv = _conv_of(m)
            found = True
    if found:
        with open(MSGS, "w") as f:
            for m in rows:
                f.write(json.dumps(m) + "\n")
        _append({"id": uuid.uuid4().hex[:8], "ts": _now(), "from": "owner",
                 "conv": conv, "kind": "answer", "text": text, "ref": qid})
    return found
