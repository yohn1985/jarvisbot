#!/usr/bin/env python3
"""
The kernel: ONE bounded tick. The kernel is dumb and reliable; the intelligence
is the (eventual) single 'mind' call per tick. This stub runs in SHADOW mode on
the stdlib alone, so `./install.sh breathe` works before anything is installed.

    wake -> perceive -> orient -> decide(priority ladder) -> act -> reflect
"""
from __future__ import annotations
import sys, datetime
sys.path.insert(0, __file__.rsplit("/jarvis/", 1)[0])
from jarvis.config import load

def perceive(cfg) -> dict:
    # Real perception lives in jarvis/perceive.py (bounded, source-degrades-safe).
    # Falls back to an empty world if that module isn't present yet.
    try:
        from jarvis.perceive import perceive as _real
        return _real(cfg)
    except Exception:
        return {"active_incident": None, "unfinished_wip": None,
                "self_caused_regression": None, "needs_human_backlog": [],
                "stalest_area": "environment"}

def orient(cfg, world) -> str:
    # Rebuild "who am I" from memory. Stub: identity from config.
    return f"I am {cfg['identity']['name']} ({cfg['identity'].get('mode','shadow')} mode)."

def decide(cfg, world) -> tuple[str, str]:
    """Pick the highest non-empty rung of the priority ladder -> (rung, action)."""
    ladder = {
        "p0_active_incident":        world.get("active_incident"),
        "p1_unfinished_wip":         world.get("unfinished_wip"),
        "p2_self_caused_regression": world.get("self_caused_regression"),
        "p3_needs_human_backlog":    world.get("needs_human_backlog") or None,
        "p4_self_maintenance":       world.get("self_maintenance"),
        "p5_curiosity":              world.get("stalest_area"),
    }
    for rung in cfg["priorities"]:
        if ladder.get(rung):
            return rung, _action_for(rung, ladder[rung])
    return "p5_curiosity", "explore an unknown area"

def _action_for(rung, payload):
    return {
        "p0_active_incident":        f"resolve incident: {payload}",
        "p1_unfinished_wip":         f"continue WIP: {payload}",
        "p2_self_caused_regression": f"fix my own regression: {payload}",
        "p3_needs_human_backlog":    f"take one needs-human issue and run deep-fix",
        "p4_self_maintenance":       f"improve my own tooling/process: {payload}",
        "p5_curiosity":              f"explore stalest area: {payload}",
    }.get(rung, str(payload))

def tick(cfg) -> dict:
    world = perceive(cfg)
    who = orient(cfg, world)
    rung, action = decide(cfg, world)
    mode = cfg["identity"].get("mode", "shadow")
    decision = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "who": who, "rung": rung, "action": action, "mode": mode,
                "would_execute": mode != "shadow",
                "backlog": world.get("_backlog_count", 0)}
    think(cfg, decision, world)
    if not decision.get("asked"):          # if we asked the owner, wait for an answer — don't act
        act(cfg, decision, world)
    # record the tick so the dashboard can show it (best-effort)
    try:
        from jarvis.runtime import record
        record(mode=f"tick/{rung}", target=action[:60], pool="kernel",
               model=decision.get("model", "-"),
               status="would" if mode == "shadow" else "ok",
               thought=decision.get("thought", ""), asked=decision.get("asked", ""),
               worker=decision.get("worker", ""))
    except Exception:
        pass
    try:                                   # reflect: write this experience to feedback memory
        from jarvis import feedback
        feedback.record(cfg, kind="tick", area=rung, summary=action[:120],
                        outcome=(decision.get("worker") or decision.get("asked")
                                 or decision.get("thought") or "")[:160])
    except Exception:
        pass
    return decision

def _main_brain_is_frontier(cfg):
    """Is the MAIN reasoning brain (orchestrator role) a frontier-grade backend (claude/codex)?
    Local/agent-tier models (ollama) are fine for spawned agents, but not Jarvis's best thinking."""
    llm = cfg.get("llm", {}) or {}
    backend = ((llm.get("routing", {}) or {}).get("orchestrator", "")).split(":", 1)[0]
    return backend in set(llm.get("frontier_backends", ["claude", "codex"]))


def _maybe_ask_upgrade(cfg):
    """Jarvis's desire to think better: if its main brain isn't frontier-grade, ask the owner ONCE
    to add a stronger one (keeping the cheap model for spawned agents)."""
    if _main_brain_is_frontier(cfg):
        return
    from pathlib import Path
    marker = Path(__file__).resolve().parent.parent / "state" / "brain_upgrade.json"
    if marker.exists():
        return
    try:
        from jarvis import messaging
        messaging.post_note(
            "Heads up: my MAIN reasoning runs on a local/agent-tier model right now — great for the "
            "grunt work my sub-agents do, but I'd think noticeably better with a frontier model. If "
            "you have a Claude (Max/Pro) or ChatGPT/Codex subscription, add it as my main brain "
            "(`claude setup-token` or `codex login`) and I'll reason with it while keeping the cheap "
            "model for spawned agents.", conv="suggestions", title="Suggestions")
        marker.parent.mkdir(exist_ok=True)
        marker.write_text("asked")
    except Exception:
        pass


def _open_question_count():
    """How many discovery questions Jarvis still hasn't answered (drives learning cadence)."""
    import json
    from pathlib import Path
    try:
        qf = Path(__file__).resolve().parent.parent / "workspace" / "knowledge" / "questions.json"
        return sum(1 for x in json.loads(qf.read_text()) if not x.get("answered"))
    except Exception:
        return 0


# How many questions to chew through per curiosity cycle. >1 so Jarvis doesn't sit on the same open
# questions for hours, but small enough that a tick stays bounded (each answer is ~2 Opus calls).
# The loop also wakes faster while a learning backlog remains (see loop.next_delay).
_ANSWER_PER_CYCLE = 2


def _explore(cfg, decision):
    """Autonomous curiosity CYCLE: if there are open questions, answer several this cycle (build
    understanding fast); otherwise (re)discover when the picture is stale, which queues fresh
    questions. So it discovers -> wonders -> answers -> keeps building. Read-only; runs in shadow."""
    import json, subprocess, time as _t
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    py = root / ".venv" / "bin" / "python"
    py = str(py) if py.exists() else "python3"
    skill = str(root / "skills" / "discover" / "skill.py")

    ex = cfg.get("explore", {}) or {}
    ans_to = int(ex.get("answer_timeout", 240))
    per = int(ex.get("answers_per_cycle", _ANSWER_PER_CYCLE))
    per = max(1, min(per, 540 // max(30, ans_to)))   # keep per × timeout under the ~600s tick-lock TTL

    def run(args):
        # think() is gated off during learning so a curiosity tick stays bounded and can't outlive
        # its lock (per × ans_to is clamped under the 600s tick-lock TTL above).
        subprocess.run([py, skill, *args], capture_output=True, text=True, timeout=ans_to, cwd=str(root))

    # Routine curiosity is SILENT — it shows in the RUNS feed (decision['worker']); chat is reserved
    # for things the owner should see (suggestions, questions, problems) so it isn't spammed.
    open_qs = _open_question_count()

    if open_qs > 0:                       # learning: answer several open questions this cycle
        answered = 0
        for _ in range(min(open_qs, per)):
            try:
                run(["--answer-one"])
                answered += 1
            except Exception:
                break
        remaining = _open_question_count()
        decision["worker"] = f"curiosity: answered {answered} question(s), {remaining} left"
        decision["learning_backlog"] = remaining     # >0 -> loop wakes sooner to keep learning
        return

    marker = root / "state" / "explore.json"            # no open questions -> rediscover if stale
    interval = int((cfg.get("explore", {}) or {}).get("interval_seconds", 21600))   # default 6h
    try:
        last = json.loads(marker.read_text()).get("ts", 0)
    except Exception:
        last = 0
    if _t.time() - last >= interval:
        try:
            run(["--network"])
            marker.parent.mkdir(exist_ok=True)
            marker.write_text(json.dumps({"ts": _t.time()}))
            decision["worker"] = "curiosity: explored + queued new questions"
        except Exception as e:
            decision["worker"] = f"(explore failed: {str(e)[:50]})"
        return

    # caught up (questions answered, discovery fresh) -> propose work (gated: only when knowledge grew)
    try:
        run(["--suggest"])
        decision["worker"] = "curiosity: reviewed knowledge / proposed work"
    except Exception as e:
        decision["worker"] = f"(suggest failed: {str(e)[:50]})"
    _maybe_ask_upgrade(cfg)             # desire a better main brain (asks once if agent-tier)


def act(cfg, decision, world):
    """Route the decided rung to a worker — Jarvis's hands. The runner enforces propose-only:
    it only executes when mode!=shadow AND the worker's action_class is 'allow'; otherwise it
    records the INTENT. So this is always safe to call."""
    rung = decision["rung"]
    if rung == "p5_curiosity":          # autonomous discovery + documentation (read-only)
        _explore(cfg, decision)
        return
    backlog = world.get("needs_human_backlog") or []
    plan = {
        "p3_needs_human_backlog":    ("deep_fix", backlog[0] if backlog else None),
        "p2_self_caused_regression": ("deep_fix", world.get("self_caused_regression")),
        "p1_unfinished_wip":         ("fixer", world.get("unfinished_wip")),
    }.get(rung)
    if not plan or not plan[1]:
        return
    name, target = plan
    try:
        from jarvis.workers.runner import run_worker
        res = run_worker(cfg, name, target, mode=decision["mode"])
        if res.get("ok"):
            decision["worker"] = f"{name}({str(target)[:40]})" + (" [would]" if res.get("would") else " [running]")
        else:
            decision["worker"] = f"(worker {name} blocked: {res.get('reason')})"
    except Exception as e:
        decision["worker"] = f"(worker error: {str(e)[:60]})"

def think(cfg, decision, world):
    """The 'mind' pass: reason about the decision via the LLM router; if the model can't
    proceed without info only the owner has, ask through the dashboard. Degrades to no-op."""
    import os
    if os.environ.get("JARVIS_NO_THINK"):   # fitness/eval runs must be deterministic + LLM-free
        return
    if not (cfg.get("llm", {}) or {}).get("think_on_tick"):
        return
    # Don't spend the expensive main brain reflecting on every routine learning tick — when Jarvis is
    # just chewing its question queue, the answering IS the work. Reflect on Opus only when caught up
    # (queue empty) or facing real work, where the judgment actually matters.
    if decision.get("rung") == "p5_curiosity" and _open_question_count() > 0:
        return
    try:
        from jarvis.adapters.llm import build_llm
        from jarvis import messaging, persona
    except Exception:
        return
    llm = build_llm(cfg)
    if not llm:
        return
    recurring = [r.get("sig") for r in world.get("_ledger", {}).get("recurring", [])][:3]
    open_qs = _open_question_count()
    # The tick reasons on the MAIN brain (orchestrator), in character — this is Jarvis thinking,
    # not a cheap triage paraphrase. Persona makes it driven + skeptical instead of a narrator.
    prompt = (
        persona.system(cfg) + "\n\n"
        f"This is an autonomous {decision['mode']} tick. You triaged to rung={decision['rung']} "
        f"-> {decision['action']}.\n"
        f"Open backlog: {world.get('_backlog_count', 0)}. Questions you're still chasing: {open_qs}. "
        f"Recurring issues in memory: {recurring}.\n\n"
        "Think like the owner of this system, not a narrator. In 2-3 sharp, specific sentences: is this "
        "the right next move, and what is the concrete first step? Be skeptical; don't restate the "
        "obvious or pad. If you genuinely cannot proceed without information ONLY the human owner has, "
        "INSTEAD reply with exactly one line starting 'QUESTION: ' followed by your question."
    )
    try:
        out = llm.run("orchestrator", prompt, timeout=150).strip()
    except Exception as e:
        decision["thought"] = f"(no LLM: {str(e)[:80]})"
        return
    decision["model"] = (cfg.get("llm", {}).get("routing", {}) or {}).get("orchestrator", "")
    if out.upper().startswith("QUESTION:"):
        q = out.split(":", 1)[1].strip()
        posted = False
        try:
            messaging.post_question(q, ref=decision["action"][:60])   # dashboard (canonical)
            posted = True
        except Exception:
            pass
        try:                                                          # + Telegram if configured
            from jarvis.adapters.notifier import TelegramNotifier
            tg = TelegramNotifier()
            if tg.enabled:
                tg.ask(q)
        except Exception:
            pass
        # Only treat the tick as "asked" (which SKIPS act()) if the question actually reached the
        # owner. A failed post must NOT silently park the loop waiting on a question nobody saw.
        if posted:
            decision["asked"] = q
            decision["thought"] = f"stuck -> asked owner: {q}"
        else:
            decision["thought"] = f"(could not deliver question to owner; proceeding): {q[:120]}"
    else:
        decision["thought"] = out[:600]

def main():
    cfg = load()
    d = tick(cfg)
    print(f"\n  \033[36m●\033[0m {d['who']}")
    print(f"    tick {d['ts']}")
    print(f"    decided rung : {d['rung']}")
    print(f"    action       : {d['action']}")
    verb = "WOULD (shadow — not executing)" if not d["would_execute"] else "EXECUTING"
    print(f"    {verb}\n")

if __name__ == "__main__":
    main()
