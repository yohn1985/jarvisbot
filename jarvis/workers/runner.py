"""Worker runner — Jarvis's hands.

Config-driven so the core stays generic: each worker is a command template + the
action_class it requires. Propose-only/gated by construction — a worker EXECUTES only when
mode != 'shadow' AND its action_class resolves to 'allow'; otherwise the intent is recorded
(so the dashboard shows what Jarvis WOULD do) and nothing runs. Spawned workers detach and
report into the run records the dashboard reads.
"""
from __future__ import annotations
import subprocess
from jarvis.runtime import record


def gate(cfg: dict, action_class: str) -> str:
    return (cfg.get("action_classes") or {}).get(action_class, "deny")


def run_worker(cfg: dict, name: str, target: str, mode: str = "shadow") -> dict:
    w = (cfg.get("workers") or {}).get(name)
    if not w:
        return {"ok": False, "reason": f"no such worker '{name}'"}
    ac = w.get("action_class", "propose")
    g = gate(cfg, ac)
    cmd = [a.replace("{target}", str(target)) for a in w["cmd"]]
    would = (mode == "shadow") or (g != "allow")
    if would:
        record(mode=f"worker/{name}", target=str(target), pool="worker", model="-",
               status="would", note=f"gated:{ac}={g} mode={mode}")
        return {"ok": True, "would": True, "cmd": cmd, "gate": g, "action_class": ac}
    record(mode=f"worker/{name}", target=str(target), pool="worker", status="running")
    try:
        subprocess.Popen(cmd, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "would": False, "cmd": cmd}
    except Exception as e:
        record(mode=f"worker/{name}", target=str(target), pool="worker",
               status="failed", note=str(e)[:120])
        return {"ok": False, "reason": str(e)}
