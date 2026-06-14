"""Self-modification seatbelt (Darwin Gödel Machine, with reward-hacking defenses).

Rules that must never be relaxed:
  - The mind NEVER writes its own fitness/eval results (DGM fabricated logs to
    fake success). Eval runs in a sandbox the mind cannot touch.
  - Keep an ARCHIVE of versions (stepping stones), not a single mutating blob —
    enables rollback AND escaping local optima.
  - A self-edit is adopted ONLY if it (1) beats the eval set, (2) survives an
    independent red-team, (3) is live-verified (observed, not self-reported).
"""
def gate(action_class: str, cfg: dict) -> str:
    """Return 'allow' | 'ask' | 'deny' for an action class."""
    return cfg.get("action_classes", {}).get(action_class, "deny")

def propose_self_edit(*a, **k): raise NotImplementedError  # sandbox -> eval -> red-team -> adopt|rollback
