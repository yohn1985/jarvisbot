"""Config loader: DEFAULTS <- config.example.yaml (documented base) <- config.yaml (your
deltas) <- .env (secrets). Deep-merged, so your config.yaml stays tiny. Degrades gracefully
(runs on DEFAULTS alone if pyyaml is absent)."""
from pathlib import Path

DEFAULTS = {
    "identity": {"name": "Jarvis", "mode": "shadow"},
    "priorities": [   # young Jarvis: self + learning first; production backlog (p3) last
        "p0_active_incident", "p1_unfinished_wip", "p2_self_caused_regression",
        "p4_self_maintenance", "p5_curiosity", "p3_needs_human_backlog",
    ],
    "action_classes": {"investigate": "allow", "propose": "allow", "fix_pr": "ask",
                       "deploy": "deny", "infra_mutate": "deny", "self_modify": "deny"},
}

def _merge(base: dict, over: dict) -> dict:
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base

def load(root: str | None = None) -> dict:
    import os
    root = Path(root or Path(__file__).resolve().parent.parent)
    try:                       # load .env secrets (e.g. OLLAMA_API_KEY) into the environment
        from jarvis.bootstrap import secrets
        secrets.load_env()
    except Exception:
        pass
    cfg = dict(DEFAULTS)
    try:
        import yaml  # optional; shadow mode runs without it
    except Exception:
        return cfg
    extras = Path(os.environ.get("JARVIS_EXTRAS", root / "extras"))
    # precedence: DEFAULTS <- example (generic) <- config.yaml (local) <- extras/config.yaml (private overlay)
    for p in (root / "config.example.yaml", root / "config.yaml", extras / "config.yaml"):
        if p.exists():
            try:
                _merge(cfg, yaml.safe_load(p.read_text()) or {})
            except Exception:
                pass
    return cfg
