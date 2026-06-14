"""Real perception: build the world from durable signals — bounded, summarize-don't-ingest.

Stdlib-only and standalone-safe (any source that errors degrades to empty, never crashes
the tick). Sources:
  - the loopback ledger (episodic memory): recurring signatures = "this keeps costing me"
  - the worksource adapter (folder | gitea): the open backlog
  - own run records: unfinished/failed ticks
The kernel maps these onto the priority ladder.
"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _default_ledger() -> str | None:
    for c in ("/home/yohn/nightly-audit/loopback/ledger.jsonl", str(ROOT / "state" / "ledger.jsonl")):
        if Path(c).exists():
            return c
    return None


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
    ws = cfg.get("worksource", {}) or {}
    kind = ws.get("kind", "folder")
    try:
        if kind == "folder":
            d = ROOT / ws.get("path", "./tasks").lstrip("./")
            return [f.name for f in d.glob("*") if f.is_file() and f.name != ".keep"] if d.exists() else []
        if kind == "gitea":
            return _gitea_open(ws)
    except Exception:
        return []
    return []


def _gitea_open(ws: dict) -> list[str]:
    """Optional, config-gated. Reads open finding titles. Best-effort; never raises out."""
    import subprocess, urllib.request
    base = ws.get("api", "http://10.6.112.9:3000/api/v1")
    repo = ws.get("repo", "superadmin/leedagent-findings")
    tok_cmd = ws.get("token_cmd", "/home/yohn/nightly-audit/gitea-token.sh")
    try:
        tok = subprocess.run([tok_cmd], capture_output=True, text=True, timeout=10).stdout.strip()
        req = urllib.request.Request(
            f"{base}/repos/{repo}/issues?state=open&type=issues&limit=15&labels=status:auto-fixable",
            headers={"Authorization": f"token {tok}"})
        data = json.load(urllib.request.urlopen(req, timeout=10))
        return [i.get("title", "")[:70] for i in data][:15]
    except Exception:
        return []


def perceive(cfg: dict) -> dict:
    led = ledger_signals(cfg)
    backlog = worksource(cfg)
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
        "stalest_area": "telephony/whatsapp",          # TODO: knowledge_map staleness
        "_ledger": led,
        "_backlog_count": len(backlog),
    }
