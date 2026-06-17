"""Jarvis <-> owner messages, threaded into CONVERSATIONS (Gemini-style sidebar).

Append-only JSONL. Each message carries a `conv` (conversation id) + `conv_title`. Jarvis
threads its questions by topic (slug of the ref), so repeated questions about the same thing
land in one conversation. The owner can start a New chat. Stdlib only.
"""
from __future__ import annotations
import json, os, re, time, uuid
from contextlib import contextmanager
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / "state"
MSGS = STATE / "messages.jsonl"
CONV_META = STATE / "conv_meta.json"   # per-conversation flags (archived) — never deletes messages
_LOCKFILE = STATE / ".messages.lock"


@contextmanager
def _flock():
    """Cross-PROCESS exclusive lock for message read-modify-write. The dashboard and the loop both
    write messages.jsonl; without this, a whole-file rewrite (stream/answer) racing an append loses
    the append or yields a torn read. flock serializes them across processes (Linux)."""
    STATE.mkdir(exist_ok=True)
    f = open(_LOCKFILE, "w")
    try:
        try:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
        except Exception:
            pass                            # non-flock platforms: degrade to no cross-proc lock
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_UN)
        except Exception:
            pass
        f.close()


def _meta() -> dict:
    if CONV_META.exists():
        try:
            return json.loads(CONV_META.read_text())
        except Exception:
            return {}
    return {}


def _save_meta(m: dict) -> None:
    STATE.mkdir(exist_ok=True)
    CONV_META.write_text(json.dumps(m, indent=2))


def archive(conv: str, archived: bool = True) -> None:
    """Archive/unarchive a conversation — kept in storage (Jarvis still reads + auto-cleans it),
    just hidden from the active sidebar."""
    m = _meta()
    m.setdefault(conv, {})["archived"] = bool(archived)
    _save_meta(m)


def delete_conv(conv: str) -> int:
    """Permanently delete all messages for a conversation and remove its metadata entry.
    Returns the number of messages deleted."""
    deleted = 0

    def fn(rows):
        nonlocal deleted
        keep = [r for r in rows if r.get("conv") != conv]
        deleted = len(rows) - len(keep)
        rows[:] = keep
        return deleted > 0

    _mutate(fn)
    m = _meta()
    m.pop(conv, None)
    _save_meta(m)
    return deleted


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:48]
    return s or uuid.uuid4().hex[:8]


def _append(row: dict) -> dict:
    STATE.mkdir(exist_ok=True)
    with _flock():                          # serialize with whole-file rewrites so appends aren't lost
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
    """One entry per thread: id, title, last_ts, count, open_q, archived."""
    meta = _meta()
    convs: dict[str, dict] = {}
    for m in _all():
        c = _conv_of(m)
        e = convs.setdefault(c, {"id": c, "title": c, "last_ts": m["ts"], "count": 0,
                                 "open_q": 0, "archived": bool(meta.get(c, {}).get("archived"))})
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


def say(text: str, conv: str = "general", title: str | None = None, images: list | None = None) -> dict:
    return _append({"id": uuid.uuid4().hex[:8], "ts": _now(), "from": "owner",
                    "conv": conv, "conv_title": title or (text[:48] if conv != "general" else None),
                    "kind": "message", "text": text, "ref": "", "images": images or []})


def reply(text: str, conv: str = "general") -> dict:
    """Jarvis's reply to the owner in a conversation. No conv_title, so it never renames the thread."""
    return _append({"id": uuid.uuid4().hex[:8], "ts": _now(), "from": "jarvis",
                    "conv": conv, "kind": "message", "text": text, "ref": ""})


def stream_start(conv: str = "general") -> str:
    """Begin a streamed Jarvis reply: an empty message marked streaming. Returns its id."""
    mid = uuid.uuid4().hex[:8]
    _append({"id": mid, "ts": _now(), "from": "jarvis", "conv": conv,
             "kind": "message", "text": "", "thinking": "", "evidence": "", "ref": "", "streaming": True})
    return mid


def _rewrite(rows: list[dict]) -> None:
    """ATOMIC whole-file write (tmp + os.replace) — a crash mid-write can't truncate the history.
    Caller MUST already hold _flock() (do not nest _flock — flock would self-deadlock on a 2nd fd)."""
    tmp = MSGS.with_name(MSGS.name + ".tmp")
    with open(tmp, "w") as f:
        for m in rows:
            f.write(json.dumps(m) + "\n")
    os.replace(tmp, MSGS)


def _mutate(fn) -> bool:
    """Read-modify-write the whole log atomically under the cross-process lock, so a concurrent
    append (or another mutator) can never be lost between the read and the rewrite."""
    with _flock():
        rows = _all()
        changed = fn(rows)
        if changed:
            _rewrite(rows)
    return bool(changed)


def stream_update(mid: str, text: str, thinking: str | None = None, evidence: str | None = None) -> None:
    """Set the running text, model thinking, and internal evidence of a streaming message."""
    def fn(rows):
        for m in rows:
            if m.get("id") == mid:
                m["text"] = text
                if thinking is not None:
                    m["thinking"] = thinking
                if evidence is not None:
                    m["evidence"] = evidence
                return True
        return False
    _mutate(fn)


def finalize_orphaned_streams() -> int:
    """Clear the streaming flag on any message still marked streaming. A streamed reply cannot
    survive a process restart, so on boot a streaming=True message is orphaned — without this it
    shows a frozen hanging cursor forever. Returns how many were finalized."""
    n = {"c": 0}
    def fn(rows):
        changed = False
        for m in rows:
            if m.get("streaming"):
                m["streaming"] = False
                if not (m.get("text") or "").strip():
                    m["text"] = ((m.get("text") or "") + "  ⏹ (interrupted)").strip()
                n["c"] += 1
                changed = True
        return changed
    _mutate(fn)
    return n["c"]


def stream_end(mid: str, text: str | None = None, thinking: str | None = None, evidence: str | None = None) -> None:
    """Finalize a streamed message (clears the streaming flag / cursor)."""
    def fn(rows):
        for m in rows:
            if m.get("id") == mid:
                if text is not None:
                    m["text"] = text
                if thinking is not None:
                    m["thinking"] = thinking
                if evidence is not None:
                    m["evidence"] = evidence
                m["streaming"] = False
                return True
        return False
    _mutate(fn)


def answer(qid: str, text: str) -> bool:
    box = {"conv": "general"}

    def fn(rows):
        found = False
        for m in rows:
            if m.get("id") == qid and m.get("kind") == "question":
                m["answered"], m["answer"] = True, text
                box["conv"] = _conv_of(m)
                found = True
        return found

    found = _mutate(fn)
    if found:
        _append({"id": uuid.uuid4().hex[:8], "ts": _now(), "from": "owner",
                 "conv": box["conv"], "kind": "answer", "text": text, "ref": qid})
    return found
