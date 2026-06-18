"""Durable, sanitized timing trace for Jarvis chat turns."""
from __future__ import annotations

import json
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACE_DIR = ROOT / "state" / "chat_traces"
_SECRET_KEY_RE = re.compile(r"(token|secret|password|passwd|api[_-]?key|auth|credential)", re.I)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+|"
    r"([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|AUTH)[A-Z0-9_]*=)[^\s'\"&]+"
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _redact_string(value: str, max_len: int = 700) -> str:
    value = _SECRET_VALUE_RE.sub(lambda m: (m.group(1) or m.group(2) or "") + "[redacted]", value)
    if len(value) > max_len:
        return value[:max_len] + f"... [truncated {len(value) - max_len} chars]"
    return value


def sanitize(value, max_len: int = 700):
    """Keep trace logs useful without storing secrets or giant payloads."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_string(value, max_len=max_len)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            k = str(key)
            out[k] = "[redacted]" if _SECRET_KEY_RE.search(k) else sanitize(val, max_len=max_len)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize(v, max_len=max_len) for v in list(value)[:30]]
    return _redact_string(repr(value), max_len=max_len)


class ChatRunTrace:
    """Append-only JSONL trace for a single dashboard reply."""

    def __init__(self, conv: str, owner_preview: str = "", run_id: str | None = None):
        self.run_id = run_id or f"chat-{uuid.uuid4().hex[:12]}"
        self.conv = conv or "general"
        self.message_id = ""
        self.started = time.monotonic()
        self.path = TRACE_DIR / f"{self.run_id}.jsonl"
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        self.event("chat_start", conv=self.conv, owner_preview=owner_preview[:240])

    def bind_message(self, message_id: str) -> None:
        self.message_id = message_id or ""
        self.event("message_bound", message_id=self.message_id)

    def duration_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def event(self, name: str, **data) -> None:
        row = {
            "ts": _now(),
            "run_id": self.run_id,
            "conv": self.conv,
            "message_id": self.message_id,
            "elapsed_ms": self.duration_ms(),
            "event": name,
        }
        if data:
            row.update(sanitize(data))
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(row, sort_keys=True) + "\n")
        except Exception:
            pass

    @contextmanager
    def span(self, name: str, **data):
        start = time.monotonic()
        self.event(f"{name}_start", **data)
        try:
            yield
        except Exception as exc:
            self.event(
                f"{name}_error",
                duration_ms=int((time.monotonic() - start) * 1000),
                error=str(exc)[:300],
            )
            raise
        else:
            self.event(f"{name}_end", duration_ms=int((time.monotonic() - start) * 1000))

    def finish(self, status: str = "ok", **data) -> None:
        self.event("chat_finish", status=status, duration_ms=self.duration_ms(), **data)


def relative_trace_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except Exception:
        return str(path)
