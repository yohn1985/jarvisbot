# Jarvis

A standalone, self-improving agent runtime: it wakes on a loop, perceives its
world from durable memory, decides ONE bounded thing to do, spawns workers,
audits itself, and writes back what it learned. A "sleeper" that rebuilds its
context every wake; a swarm coordinated through shared memory.

**The installer (`install.sh`) is the source of truth.** To change anything,
edit the matching `gen_*`/heredoc in `install.sh` and re-run `./install.sh`.

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

## Quickstart
```
./install.sh            # scaffold + check deps
./install.sh breathe    # watch one shadow tick (no services needed)
./install.sh up         # start postgres + redis
./install.sh initdb     # apply the ai_memory schema
```

MIT licensed.
