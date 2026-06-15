"""Run records: the kernel + workers append events here; the dashboard reads them.
Append-only JSONL under state/. Stdlib only."""
from __future__ import annotations
import json, time, uuid
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / "state"
RUNS = STATE / "runs.jsonl"


def record(mode: str, target: str = "", pool: str = "kernel", model: str = "-",
           status: str = "ok", duration=None, **extra) -> dict:
    STATE.mkdir(exist_ok=True)
    row = {"id": uuid.uuid4().hex[:8],
           "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "mode": mode, "target": target, "pool": pool, "model": model,
           "status": status, "duration": duration, **extra}
    with open(RUNS, "a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def recent(limit: int = 200) -> list[dict]:
    if not RUNS.exists():
        return []
    rows = []
    for line in RUNS.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows[-limit:][::-1]
