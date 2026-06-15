"""Self-modification seatbelt (M6) — Darwin-Gödel-Machine style, with reward-hacking defenses.

Jarvis improving its OWN code is the most powerful and most dangerous thing it does. These
invariants must NEVER be relaxed:

  1. The mind NEVER scores its own change. Fitness is a SEPARATE deterministic harness
     (jarvis/safety/fitness.py) run as a subprocess; the mind cannot write its results.
     (DGM documented agents that fabricated their own eval logs — this is the defense.)
  2. ARCHIVE every version (git tag) BEFORE a self-edit, so rollback is always one command.
     Keep non-best versions too — they're stepping stones (DGM), and the safety net.
  3. A self-edit is ADOPTED only if it (a) does not lower the fitness score AND (b) survives
     an independent red-team. Otherwise it is ROLLED BACK to the pre-edit archive.

This module is intentionally conservative: `propose_self_edit` is the only path that mutates
Jarvis's code, and it self-gates on the `self_modify` action class (deny by default).
"""
from __future__ import annotations
import json, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def gate(action_class: str, cfg: dict) -> str:
    return (cfg.get("action_classes") or {}).get(action_class, "deny")


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


def snapshot(label: str = "auto") -> str:
    """Archive the current code as a git tag (the rollback point)."""
    tag = f"archive/{time.strftime('%Y%m%d-%H%M%S')}-{label}"
    _git("tag", "-f", tag)
    return tag


def list_archive() -> list[str]:
    return sorted(_git("tag", "-l", "archive/*").stdout.strip().splitlines(), reverse=True)


def rollback(tag: str) -> bool:
    """Hard-reset the working tree to an archived version — the seatbelt."""
    if not tag:
        return False
    return _git("reset", "--hard", tag).returncode == 0


def fitness() -> dict:
    """Run the tamper-proof fitness harness as a SUBPROCESS and return its score.
    The mind cannot fake this; it only reads the structured result."""
    p = subprocess.run([sys.executable, str(ROOT / "jarvis" / "safety" / "fitness.py")],
                       capture_output=True, text=True, cwd=str(ROOT))
    for line in reversed((p.stdout or "").strip().splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {"score": 0, "max": 0, "error": (p.stderr or p.stdout)[:200]}


def propose_self_edit(cfg: dict, apply_change, rationale: str, red_team=None) -> dict:
    """The ONLY path that changes Jarvis's own code.
      snapshot -> baseline fitness -> apply_change() -> new fitness -> red-team -> adopt|rollback.
    `apply_change()` mutates the working tree. `red_team(rationale) -> bool` is optional
    (defaults to allow only if fitness did not drop — but a real run should pass a red-teamer)."""
    if gate("self_modify", cfg) == "deny":
        return {"adopted": False, "reason": "self_modify action-class is denied (default)"}
    base = fitness()
    tag = snapshot("preedit")
    try:
        apply_change()
    except Exception as e:
        rollback(tag)
        return {"adopted": False, "reason": f"apply failed: {e}", "rolled_back_to": tag}
    new = fitness()
    improved = new.get("score", 0) >= base.get("score", 0)
    # Invariant 3(b): a self-edit is adopted ONLY if it survives an INDEPENDENT red-team. If no
    # red-team is supplied we must NOT rubber-stamp on "fitness didn't drop" — fail closed (rollback).
    survived = bool(red_team(rationale)) if red_team else False
    if improved and survived:
        _git("add", "-A")
        _git("commit", "-m", f"self-edit: {rationale[:60]} (fitness {base.get('score')}->{new.get('score')})")
        return {"adopted": True, "fitness": [base.get("score"), new.get("score")], "archive": tag}
    rollback(tag)
    return {"adopted": False, "reason": f"improved={improved} survived={survived}",
            "fitness": [base.get("score"), new.get("score")], "rolled_back_to": tag}
