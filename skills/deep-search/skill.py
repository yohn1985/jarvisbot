#!/usr/bin/env python3
"""deep-search — Jarvis's super-searcher (public distillation of the deep-fix method).

LOCATE (LLM) -> TRACE (ripgrep) -> EXPLORE (LLM, parallel) -> SYNTHESIZE (LLM) -> RED-TEAM (LLM).
Runs on Jarvis's LLM router + ripgrep only; no environment coupling.

    python skill.py "the bug or question" [--repos /a,/b] [--leaves 4]
"""
from __future__ import annotations
import argparse, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from jarvis.config import load
from jarvis.adapters.llm import build_llm


def rg(pattern: str, repos: list[str], cap: int = 80) -> str:
    try:
        out = subprocess.run(["rg", "-n", "--no-heading", "-m", "30", "--max-columns", "200",
                              "-e", pattern, *repos], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        out = ""
    return "\n".join(out.splitlines()[:cap])


def locate(llm, question: str) -> list[str]:
    p = (f"Find the CENTER of the blast radius for this bug/question:\n{question}\n\n"
         "Return ONLY a comma-separated list of 3-8 EXACT identifiers (function/field/route/error "
         "names) to trace across the code. No prose.")
    raw = llm.run("orchestrator", p, timeout=90)
    return [s.strip() for s in raw.replace("\n", ",").split(",") if s.strip()][:8]


def explore(llm, thread_q: str, trace: str) -> str:
    p = (f"You are a researcher. Answer precisely, citing file:line.\nQUESTION: {thread_q}\n\n"
         f"TRACE:\n{trace[:6000]}\n\nReturn dense JSON: "
         '{"answer":"..","findings":[{"file_line":"..","what":".."}],"siblings":["file:line of same-shape sites"]}')
    return llm.run("researcher", p, timeout=120)


def synthesize(llm, question: str, trace: str, packs: str):
    plan = llm.run("orchestrator",
                   f"Synthesize a complete, root-caused plan for:\n{question}\n\nTRACE:\n{trace[:4000]}\n\n"
                   f"RESEARCH PACKS:\n{packs[:8000]}\n\nGive: root cause across ALL swept sites (file:line), "
                   "the fix, and a blast-radius note (who/what is affected).", timeout=180)
    redteam = llm.run("red_team",
                      f"PROVE THIS WRONG or INCOMPLETE — find a broken consumer, an unswept sibling, or "
                      f"that it doesn't address the root cause:\n{plan[:6000]}\n\n"
                      "End with a verdict: 'survives' or 'broken: <file:line reason>'.", timeout=120)
    return plan, redteam


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--repos", default="")
    ap.add_argument("--leaves", type=int, default=4)
    a = ap.parse_args()

    cfg = load()
    llm = build_llm(cfg)
    if not llm:
        sys.exit("deep-search: no LLM backend configured (set llm.routing/backends in config)")
    repos = [r for r in a.repos.split(",") if r] or (cfg.get("deep_search", {}) or {}).get("repos", ["."])

    print(f"[deep-search] LOCATE ...", file=sys.stderr)
    seeds = locate(llm, a.question)
    print(f"  seeds: {seeds}", file=sys.stderr)

    print(f"[deep-search] TRACE ({len(repos)} repos) ...", file=sys.stderr)
    trace = "\n".join(f"## {s}\n{rg(s, repos)}" for s in seeds)

    print(f"[deep-search] EXPLORE ({min(a.leaves, len(seeds))} threads) ...", file=sys.stderr)
    packs = [explore(llm, f"How does `{s}` relate to: {a.question}", trace) for s in seeds[:a.leaves]]

    print(f"[deep-search] SYNTHESIZE + RED-TEAM ...", file=sys.stderr)
    plan, redteam = synthesize(llm, a.question, trace, "\n---\n".join(packs))

    print("\n===== PLAN =====\n" + plan + "\n\n===== RED-TEAM =====\n" + redteam)


if __name__ == "__main__":
    main()
