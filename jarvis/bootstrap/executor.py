"""Bootstrap executor (Phase 0, step 2b) — the ONE module that changes the machine.

It takes the plan from installer.plan() plus the set of APPROVED action ids, and runs each
approved, non-blocked action in order via subprocess: capturing output, recording each as a run
row the dashboard shows, stopping on the first failure (so a broken step can't cascade). It is
idempotent — re-running re-derives the plan and only does what's still missing.

Headless-aware: an action that would need a sudo PASSWORD (non-root + sudo prefix) is deferred,
not run, unless we're attached to a terminal — so the background loop never hangs on a prompt.
Privileged/interactive setup (sudo installs, `claude login`) belongs to the interactive first-run.

Nothing here runs without an explicit approval set (or assume_yes). No AI involved.
"""
from __future__ import annotations
import subprocess, sys, time

from jarvis.bootstrap import installer
from jarvis.runtime import record


def _interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def execute(cfg: dict, approved_ids=None, *, assume_yes: bool = False,
            interactive: bool | None = None, stop_on_fail: bool = True) -> dict:
    interactive = _interactive() if interactive is None else interactive
    plan = installer.plan(cfg)
    if approved_ids is not None:
        approved = set(approved_ids)
    elif assume_yes:
        approved = {a["id"] for a in plan if not a["blocked"]}
    else:
        approved = set()

    ran, failed, skipped = [], [], []
    for a in plan:
        aid, cmd = a["id"], a["cmd"]
        if a["blocked"]:
            skipped.append((aid, "blocked: " + a["blocked_reason"]))
            continue
        if aid not in approved:
            skipped.append((aid, "not approved"))
            continue
        if cmd and cmd[0] == "sudo" and not interactive:   # would prompt for a password headless
            skipped.append((aid, "deferred: needs sudo password, no terminal"))
            record(mode=f"setup/{aid}", target=a["desc"], pool="bootstrap", status="would",
                   note="deferred: needs sudo, headless")
            continue
        print(f"[setup] {a['desc']}\n        $ {' '.join(cmd)}")
        t0 = time.time()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            ok, detail = p.returncode == 0, (p.stderr or p.stdout or "")
        except Exception as e:
            ok, detail = False, str(e)
        dur = round(time.time() - t0, 1)
        if ok:
            ran.append(aid)
            record(mode=f"setup/{aid}", target=a["desc"], pool="bootstrap", status="ok", duration=dur)
            print(f"        ok ({dur}s)")
        else:
            tail = detail.strip()[-400:]
            failed.append((aid, tail))
            record(mode=f"setup/{aid}", target=a["desc"], pool="bootstrap", status="failed",
                   duration=dur, note=tail[:200])
            print(f"        FAILED ({dur}s): {tail[:300]}")
            if stop_on_fail:
                break

    remaining = [a["id"] for a in installer.plan(cfg)]   # re-derive: what's still missing
    return {"ran": ran, "failed": failed, "skipped": skipped, "remaining": remaining}


if __name__ == "__main__":
    import argparse
    from jarvis.config import load
    ap = argparse.ArgumentParser(description="run an approved bootstrap plan")
    ap.add_argument("--yes", action="store_true", help="approve & run ALL non-blocked actions")
    ap.add_argument("--only", default="", help="comma-separated action ids to run")
    ap.add_argument("--keep-going", action="store_true", help="don't stop on first failure")
    a = ap.parse_args()
    ids = [x for x in a.only.split(",") if x] or None
    res = execute(load(), approved_ids=ids, assume_yes=a.yes, stop_on_fail=not a.keep_going)
    print("\n=== summary ===")
    print(" ran     :", res["ran"])
    print(" failed  :", res["failed"])
    print(" skipped :", [s[0] for s in res["skipped"]])
    print(" remaining:", res["remaining"])
