"""Config loader: config.yaml (non-secret) + .env (secrets). Degrades gracefully."""
import os
from pathlib import Path

DEFAULTS = {
    "identity": {"name": "Jarvis", "mode": "shadow"},
    "priorities": [
        "p0_active_incident", "p1_unfinished_wip", "p2_self_caused_regression",
        "p3_needs_human_backlog", "p4_self_maintenance", "p5_curiosity",
    ],
    "action_classes": {"investigate": "allow", "propose": "allow",
                       "fix_pr": "ask", "deploy": "deny", "infra_mutate": "deny"},
}

def load(root: str | None = None) -> dict:
    root = Path(root or Path(__file__).resolve().parent.parent)
    cfg = dict(DEFAULTS)
    path = root / "config.yaml"
    if not path.exists():
        path = root / "config.example.yaml"
    try:
        import yaml  # optional; shadow mode runs without it
        if path.exists():
            cfg.update(yaml.safe_load(path.read_text()) or {})
    except Exception:
        pass
    return cfg
