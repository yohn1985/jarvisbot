---
name: deep-search
description: Super-searcher — locate a bug/question's blast-radius center, trace it, explore in parallel, synthesize a plan, then red-team the conclusion.
entrypoint: skill.py
requires: (none beyond ripgrep)
tools: ripgrep, the Jarvis LLM router
when_to_use: a non-trivial bug/design question where the answer needs the WHOLE blast radius (all the consumers/siblings), not a single-file read — and the conclusion should be adversarially checked before you trust it.
---

# deep-search

The open-source distillation of the deep-fix method: approximate a long-horizon mind on a
finite-context model by externalizing the work.

```
LOCATE (LLM)  → the blast-radius center (seed identifiers)
TRACE  (rg)   → map the blast radius across the repos
EXPLORE (LLM) → parallel dense findings packs (one per thread)
SYNTHESIZE (LLM) → a plan across ALL paths
RED-TEAM (LLM) → prove the conclusion wrong/incomplete before trusting it
```

Runs entirely on Jarvis's configured LLM router (claude/codex/ollama) + ripgrep — no
environment coupling, so it's public. The judgment points (locate/synthesize/red-team) route to
the strong model; exploration can route cheaper.

## Usage
```
./install.sh skill run deep-search "why does add-to-waba 400 before calling Meta" --repos /path/to/repo
./install.sh skill run deep-search "<bug or design question>" --leaves 6
```
Repos default to `deep_search.repos` in config (or the current directory).

## Output
A root-caused plan (file:line across all swept sites) + a red-team verdict (`survives|broken`).
The value is the *sweep + self-audit*, not a single-file answer.
