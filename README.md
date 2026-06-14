# Jarvis

A standalone, self-improving agent runtime: it wakes on a loop, perceives its
world from durable memory, decides ONE bounded thing to do, spawns workers,
audits itself, and writes back what it learned. A "sleeper" that rebuilds its
context every wake; a swarm coordinated through shared memory.

**The `jarvis/` package is the source of truth — edit the files directly.** `install.sh`
bootstraps and runs everything (scaffolds config, checks deps, brings the dashboard up); it only
*seeds* the package when files are missing, so re-running it never overwrites your code.

## Architecture (generic core + pluggable adapters)
- `jarvis/kernel.py`   wake → perceive → orient → decide → act → reflect (one bounded tick)
- `jarvis/adapters/`   llm / memory / notifier / worksource / secrets — nothing infra-specific in core
- `jarvis/memory/`     three tiers (MemGPT-style): core (RAM) / recall / archival (disk)
- `jarvis/safety/`     DGM-style self-modification seatbelt: version archive + sandbox eval +
                       TAMPER-PROOF fitness + red-team-before-adopt + rollback
- `jarvis/workers/`    deep-fix / fixer / on-call / explorer (spawned on demand)

## Memory tiers
- Redis    = working memory (wake queue, locks, pheromone signals)
- Postgres = episodic/structured long-term (`ai_memory`: episodes, knowledge, questions, watermarks)
- Mem0/Zep = semantic/consolidated knowledge (optional adapter)

## Safety defaults (important)
PROPOSE-ONLY out of the box. Irreversible/customer-facing action classes are
OPT-IN in `config.yaml`. The mind NEVER scores its own work (reward-hacking
defense — see DGM). Self-edits go: sandbox → eval (write-protected) → red-team → adopt or rollback.

## Commands (the installer is the only entrypoint)
```
./install.sh             # scaffold the tree + check deps
./install.sh breathe     # one tick (perceive -> decide -> think -> act, shadow)
./install.sh run         # the persistent wake loop (heartbeat + self-schedule + alert-wake)
./install.sh dashboard --host 0.0.0.0 --port 8787   # the browser dashboard (chat/runs/config)
./install.sh skill list|install <n>|run <n> [args]  # skills (e.g. youtube-research)
./install.sh selfcheck   # the tamper-proof fitness score (the mind can't fake this)
./install.sh archive     # snapshot a rollback point;  ./install.sh rollback <tag>
./install.sh up          # docker: postgres + redis     ./install.sh migrate  # ledger -> postgres
./install.sh wake        # poke the loop to wake now     ./install.sh doctor   # health check
```

## Status (self-build milestones)
M1 perceive(real) · M2 LLM router · M3 episodic memory(degrade-safe) · M4 workers(hands,
propose-only) · M5 wake loop · M6 self-mod seatbelt(archive+fitness+rollback). Channels:
browser dashboard (chat w/ conversations) + Telegram. Default mode: **shadow** (proposes,
never executes); young Jarvis works on itself + learns before the production backlog.

MIT licensed. Home: jarvisbot.app
