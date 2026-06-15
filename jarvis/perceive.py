"""Real perception: build the world from durable signals — bounded, summarize-don't-ingest.

Stdlib-only and standalone-safe. A source OUTAGE is surfaced (e.g. _worksource_status) rather than
masked as "no work". Sources:
  - the loopback ledger (episodic memory): recurring signatures = "this keeps costing me"
  - the worksource adapter (folder | gitea): the open backlog
The kernel maps these onto the priority ladder.
  - active_incident / unfinished_wip: not yet wired (alertmanager + WIP/run-record adapters) — these
    rungs stay None until implemented; the loop must not pretend they exist.
"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _default_ledger() -> str | None:
    # Generic default only; env-specific ledger path comes from config (memory.episodic.ledger).
    p = ROOT / "state" / "ledger.jsonl"
    return str(p) if p.exists() else None


def ledger_signals(cfg: dict) -> dict:
    # Prefer the episodic store (postgres if up; it degrades to the same jsonl otherwise).
    try:
        from jarvis.memory.store import build_store
        s = build_store(cfg)
        return {"recurring": s.recurring(2, 5), "recent": s.recent(5), "backend": s.backend}
    except Exception:
        pass
    path = (cfg.get("memory", {}).get("episodic", {}) or {}).get("ledger") or _default_ledger()
    p = Path(path) if path else None
    if not p or not p.exists():
        return {"recurring": [], "recent": []}
    rows = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    recurring = sorted((r for r in rows if int(r.get("recurrence", 1)) >= 2),
                       key=lambda r: -int(r.get("recurrence", 1)))
    return {"recurring": recurring[:5], "recent": rows[-5:]}


def worksource(cfg: dict) -> list[str]:
    """The open backlog. RAISES on a real source outage (so the caller can tell a down worksource
    from a genuinely-empty one — they are NOT the same and must not both look like 'no work')."""
    ws = cfg.get("worksource", {}) or {}
    kind = ws.get("kind", "folder")
    if kind == "folder":
        d = ROOT / ws.get("path", "./tasks").lstrip("./")
        return [f.name for f in d.glob("*") if f.is_file() and f.name != ".keep"] if d.exists() else []
    if kind == "gitea":
        return _gitea_open(ws)
    return []


def _gitea_open(ws: dict) -> list[str]:
    """Optional, fully config-driven. api/repo/token_cmd come from config.yaml — no infra is
    hardcoded in the core. Missing config => no gitea backlog (legit empty); a fetch ERROR RAISES
    so perceive() can surface 'source down' instead of masking it as 'no work'."""
    import subprocess, urllib.request
    base = ws.get("api")
    repo = ws.get("repo")
    tok_cmd = ws.get("token_cmd")
    if not (base and repo and tok_cmd):
        return []
    tok = subprocess.run([tok_cmd], capture_output=True, text=True, timeout=10).stdout.strip()
    req = urllib.request.Request(
        f"{base}/repos/{repo}/issues?state=open&type=issues&limit=15&labels=status:auto-fixable",
        headers={"Authorization": f"token {tok}"})
    data = json.load(urllib.request.urlopen(req, timeout=10))
    return [i.get("title", "")[:70] for i in data][:15]


def perceive(cfg: dict) -> dict:
    led = ledger_signals(cfg)
    try:                                   # a down worksource must look DIFFERENT from an empty one
        backlog, ws_status = worksource(cfg), "ok"
    except Exception as e:
        backlog, ws_status = [], f"error: {str(e)[:100]}"
    recurring = led["recurring"]
    # Categorize recurring ledger signatures by their LABEL. A code-defect that keeps recurring is
    # a real regression to fix; a tooling-gap / process / friction lesson is SELF-IMPROVEMENT, not
    # something Jarvis "caused". (Jarvis's own question caught the earlier over-broad mapping.)
    code_defects = [r for r in recurring if r.get("label") == "code-defect"]
    improvements = [r for r in recurring if r.get("label") in
                    ("tooling-gap", "process", "operational-friction", "infra")]
    return {
        "active_incident": None,                       # TODO: alertmanager adapter
        "unfinished_wip": None,                        # TODO: WIP index
        "self_caused_regression": (code_defects[0].get("sig") if code_defects else None),
        "self_maintenance": (improvements[0].get("sig") if improvements else None),
        "needs_human_backlog": backlog,
        "stalest_area": "environment",                 # generic curiosity trigger; real curiosity is
                                                       # driven by the question queue + discovery staleness
        "_ledger": led,
        "_backlog_count": len(backlog),
        "_worksource_status": ws_status,               # diagnostic only (_-prefixed -> decide() ignores it)
    }
