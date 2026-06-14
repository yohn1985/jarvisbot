#!/usr/bin/env bash
# ============================================================================
#  Jarvis — install.sh   (the source of truth: edit THIS to change the system)
# ----------------------------------------------------------------------------
#  A standalone, open-source-clean, self-improving agent runtime.
#  This installer MATERIALIZES the project (config, schema, kernel, adapters)
#  via heredocs, sets up the stack, and lets the shadow kernel "breathe".
#  To change Jarvis: edit the relevant gen_* function below and re-run.
#
#  Subcommands:
#    scaffold     (default) materialize/refresh generated files (idempotent)
#    deps         check prerequisites (docker, python, claude/codex/ollama CLIs)
#    deps --install   attempt to install missing CLIs (asks first)
#    up | down    start/stop the docker stack (postgres + redis)
#    initdb       apply db/schema.sql to the ai_memory database
#    breathe      run ONE shadow-mode kernel tick (no deps, prints what it'd do)
#    doctor       health check the whole setup
#
#  Design notes live in README.md (also generated). Safety default = PROPOSE-ONLY.
# ============================================================================
set -uo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
log(){ printf '\033[36m[jarvis]\033[0m %s\n' "$*"; }
warn(){ printf '\033[33m[jarvis] WARN:\033[0m %s\n' "$*"; }
die(){ printf '\033[31m[jarvis] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# write a GENERATED file (always refreshed from this script). $1=relpath, stdin=content.
gen(){ local p="$ROOT/$1"; mkdir -p "$(dirname "$p")"; cat > "$p"; log "wrote $1"; }
# write a file ONLY if absent (never clobber user data/secrets). $1=relpath, stdin=content.
seed(){ local p="$ROOT/$1"; [ -e "$p" ] && { log "kept  $1 (exists)"; return; }; mkdir -p "$(dirname "$p")"; cat > "$p"; log "seeded $1"; }

# ---------------------------------------------------------------------------
scaffold(){
  log "materializing Jarvis skeleton under $ROOT"

  gen README.md <<'EOF'
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
EOF

  gen LICENSE <<'EOF'
MIT License

Copyright (c) 2026 Jarvis contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED. IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR
OTHER LIABILITY ARISING FROM THE USE OF THE SOFTWARE.
EOF

  gen .gitignore <<'EOF'
.env
*.pyc
__pycache__/
.venv/
output/
*.log
db/data/
config.yaml
EOF

  gen .env.example <<'EOF'
# Secrets ONLY (never commit the real .env). Non-secret config lives in config.yaml.
POSTGRES_PASSWORD=change_me
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
OLLAMA_HOST=http://localhost:11434
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
EOF

  gen config.example.yaml <<'EOF'
# Jarvis config — NON-secret, environment-specific. Copy to config.yaml and edit.
identity:
  name: Jarvis
  owner: yohn
  mode: shadow            # shadow | assist | autonomous  (start in shadow!)

# What may it DO without asking? Everything else is propose-only.
action_classes:
  investigate: allow      # read-only: always safe
  propose: allow          # write a plan / open a PR (reviewed)
  fix_pr: ask             # open a fix PR through the red-team gate
  deploy: deny            # irreversible / customer-facing → earn it
  infra_mutate: deny      # nginx/dns/firewall → never autonomous yet

# LLM = a router over a model catalog (claude / codex / ollama local+cloud).
llm:
  backends: [claude, codex, ollama]
  routing:                 # role -> model (cost/capability tiered)
    triage:        ollama:small         # the wake-tick decision can be cheap
    orchestrator:  claude:opus          # deep judgment — frontier only
    red_team:      claude:opus          # self-audit must be sharp
    fixer:         codex:gpt-5.5
    researcher:    ollama:qwen-cloud    # high-volume leaf work — cheap
    summarizer:    ollama:llama-local   # log/area summarization — cheapest
  fallbacks: [claude:opus, ollama:small]

memory:
  working:   {kind: redis,    url: redis://localhost:6379/0}
  episodic:  {kind: postgres, dsn: "postgresql://jarvis:${POSTGRES_PASSWORD}@localhost:5432/ai_memory"}
  semantic:  {kind: none}    # none | mem0 | zep   (add when needed)

notifier:
  kind: telegram             # telegram | slack | stdout
  casual: true               # talk like a colleague, not a dashboard

worksource:
  kind: folder               # folder | gitea | github
  path: ./tasks

# Wake triggers
wake:
  heartbeat_seconds: 1800    # floor so it never sleeps forever
  alert_webhook: true        # Alertmanager -> poke
  self_schedule: true        # it sets its own next-wake

# The priority ladder the tick triages by (top non-empty rung wins).
priorities:
  - p0_active_incident
  - p1_unfinished_wip
  - p2_self_caused_regression
  - p3_needs_human_backlog
  - p4_self_maintenance
  - p5_curiosity
EOF

  gen requirements.txt <<'EOF'
# Kept minimal. The shadow kernel runs on the stdlib alone.
pyyaml>=6.0
psycopg[binary]>=3.1
redis>=5.0
EOF

  gen docker-compose.yml <<'EOF'
# Standalone stateful stack. `./install.sh up` brings this online.
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: jarvis
      POSTGRES_DB: ai_memory
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-change_me}
    ports: ["5432:5432"]
    volumes: ["./db/data:/var/lib/postgresql/data"]
    restart: unless-stopped
  redis:
    image: redis:7
    ports: ["6379:6379"]
    restart: unless-stopped
EOF

  gen db/schema.sql <<'EOF'
-- ai_memory: episodic + structured long-term memory (exact-key recall).
-- pgvector is added later only where semantic search is actually needed.
CREATE TABLE IF NOT EXISTS episodes (        -- the ledger: what happened
  id          BIGSERIAL PRIMARY KEY,
  ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
  sig         TEXT NOT NULL,                 -- stable signature (exact-match recall)
  area        TEXT,                          -- service/module/host
  source      TEXT,                          -- fixer|deploy|prod|finder|operator
  label       TEXT,                          -- operational-friction|code-defect|...
  symptom     TEXT, root_cause TEXT, resolution TEXT, prevent TEXT,
  outcome     TEXT, recurrence INT NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS episodes_sig  ON episodes(sig);
CREATE INDEX IF NOT EXISTS episodes_area ON episodes(area, ts DESC);

CREATE TABLE IF NOT EXISTS knowledge (       -- what it understands about the env
  id BIGSERIAL PRIMARY KEY, area TEXT NOT NULL,
  fact TEXT NOT NULL, confidence TEXT, updated TIMESTAMPTZ DEFAULT now()
  -- embedding VECTOR(768)   -- add with pgvector when semantic recall is needed
);
CREATE TABLE IF NOT EXISTS questions (       -- the curiosity queue
  id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(),
  area TEXT, question TEXT NOT NULL, answered BOOLEAN DEFAULT false
);
CREATE TABLE IF NOT EXISTS watermarks (      -- per log-source last-read (bounded reads)
  source TEXT PRIMARY KEY, offset_or_ts TEXT, updated TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS knowledge_map (   -- areas + staleness (curiosity driver)
  area TEXT PRIMARY KEY, last_explored TIMESTAMPTZ, understanding INT DEFAULT 0
);
EOF

  gen jarvis/__init__.py <<'EOF'
"""Jarvis: a standalone self-improving agent runtime."""
__version__ = "0.0.1"
EOF

  gen jarvis/config.py <<'EOF'
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
EOF

  gen jarvis/kernel.py <<'EOF'
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
    # TODO(real): read alerts, findings, WIPs, ledger, service health — bounded,
    # by area, delta-since-watermark, summarize-then-discard. Stubbed for now.
    return {"active_incident": None, "unfinished_wip": None,
            "self_caused_regression": None, "needs_human_backlog": [],
            "stalest_area": "telephony/whatsapp"}

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
        "p4_self_maintenance":       None,
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
        "p5_curiosity":              f"explore stalest area: {payload}",
    }.get(rung, str(payload))

def tick(cfg) -> dict:
    world = perceive(cfg)
    who = orient(cfg, world)
    rung, action = decide(cfg, world)
    mode = cfg["identity"].get("mode", "shadow")
    decision = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "who": who, "rung": rung, "action": action, "mode": mode,
                "would_execute": mode != "shadow"}
    return decision

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
EOF

  seed jarvis/adapters/__init__.py <<'EOF'
"""Pluggable adapters: nothing infra-specific lives in the kernel core."""
EOF
  gen jarvis/adapters/llm.py <<'EOF'
"""LLM provider adapter = a router over a model catalog (claude/codex/ollama).
Generalizes ai-exec: role -> model, with cost/capability tiering + fallbacks."""
from abc import ABC, abstractmethod

class LLM(ABC):
    @abstractmethod
    def run(self, role: str, prompt: str, **kw) -> str: ...

class RoutingLLM(LLM):
    def __init__(self, routing: dict, backends: dict):
        self.routing, self.backends = routing, backends
    def run(self, role: str, prompt: str, **kw) -> str:
        target = self.routing.get(role) or self.routing.get("triage")
        # TODO: dispatch target ("backend:model") to the right CLI backend.
        raise NotImplementedError(f"route {role} -> {target}")
EOF
  gen jarvis/adapters/memory.py <<'EOF'
"""Memory adapter interface. Tiers wired in jarvis/memory/tiers.py."""
from abc import ABC, abstractmethod

class Memory(ABC):
    @abstractmethod
    def remember(self, **row): ...
    @abstractmethod
    def recall(self, *, sig=None, area=None, limit=8) -> list: ...
EOF
  gen jarvis/adapters/notifier.py <<'EOF'
"""Notifier adapter: how Jarvis reaches the owner (Telegram = casual, two-way)."""
from abc import ABC, abstractmethod

class Notifier(ABC):
    @abstractmethod
    def ask(self, question: str) -> str | None: ...   # casual question -> owner reply
    @abstractmethod
    def tell(self, message: str) -> None: ...
EOF
  gen jarvis/adapters/worksource.py <<'EOF'
"""Work source adapter: where tasks/issues come from (folder/gitea/github)."""
from abc import ABC, abstractmethod

class WorkSource(ABC):
    @abstractmethod
    def open_items(self) -> list: ...
EOF

  gen jarvis/memory/__init__.py <<'EOF'
"""Three-tier memory (MemGPT-style): core (RAM) / recall / archival (disk)."""
EOF
  gen jarvis/memory/tiers.py <<'EOF'
"""MemGPT-style tiers + self-editing API. The mind calls these during a tick.
core = pinned in-context (identity + current focus, self-editable);
recall = recent episodic (searchable); archival = cold consolidated knowledge."""
class CoreMemory:
    def append(self, block: str, text: str): ...      # core_memory_append
    def replace(self, block: str, old: str, new: str): ...  # core_memory_replace
class RecallMemory:
    def search(self, query: str, limit: int = 8) -> list: ...
class ArchivalMemory:
    def insert(self, text: str): ...
    def search(self, query: str, limit: int = 8) -> list: ...
EOF

  gen jarvis/safety/__init__.py <<'EOF'
"""Safety: action-class gates + the DGM-style self-modification seatbelt."""
EOF
  gen jarvis/safety/seatbelt.py <<'EOF'
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
EOF

  gen jarvis/workers/__init__.py <<'EOF'
"""Workers the kernel spawns on demand: deep-fix / fixer / on-call / explorer.
These wrap the proven tools (deep-fix v5, the fixer red-team gate, evidence)."""
EOF

  seed tasks/.keep <<'EOF'
# Drop task files here for the 'folder' worksource adapter.
EOF

  log "scaffold complete."
}

# ---------------------------------------------------------------------------
deps(){
  local install="${1:-}"
  log "checking prerequisites..."
  local missing=()
  for c in python3 docker; do command -v "$c" >/dev/null 2>&1 && log "  ok   $c" || { warn "  MISSING $c"; missing+=("$c"); }; done
  for c in claude codex ollama; do command -v "$c" >/dev/null 2>&1 && log "  ok   $c (LLM backend)" || warn "  missing $c CLI (optional backend)"; done
  command -v docker >/dev/null 2>&1 && (docker compose version >/dev/null 2>&1 && log "  ok   docker compose" || warn "  missing 'docker compose' plugin")
  [ -f "$ROOT/config.yaml" ] && log "  ok   config.yaml" || warn "  no config.yaml yet (copy from config.example.yaml)"
  [ -f "$ROOT/.env" ] && log "  ok   .env" || warn "  no .env yet (copy from .env.example, add secrets)"
  if [ "$install" = "--install" ] && [ "${#missing[@]}" -gt 0 ]; then
    warn "auto-install of system packages (${missing[*]}) is gated — run the printed commands yourself or approve explicitly."
  fi
}

initdb(){
  [ -f "$ROOT/.env" ] && set -a && . "$ROOT/.env" && set +a || true
  log "applying db/schema.sql to ai_memory..."
  docker compose -f "$ROOT/docker-compose.yml" exec -T postgres \
    psql -U jarvis -d ai_memory < "$ROOT/db/schema.sql" && log "schema applied." || warn "initdb failed (is the stack up?)"
}

up(){   [ -f "$ROOT/.env" ] || { cp "$ROOT/.env.example" "$ROOT/.env"; warn "created .env from example — set real secrets!"; }
        docker compose -f "$ROOT/docker-compose.yml" up -d && log "stack up (postgres + redis)"; }
down(){ docker compose -f "$ROOT/docker-compose.yml" down && log "stack down"; }

breathe(){ log "one shadow-mode tick:"; python3 "$ROOT/jarvis/kernel.py"; }

doctor(){ deps; log "stack:"; docker compose -f "$ROOT/docker-compose.yml" ps 2>/dev/null || warn "stack not up"; breathe; }

# ---------------------------------------------------------------------------
case "${1:-scaffold}" in
  scaffold) scaffold; echo; deps; echo; log "next: ./install.sh breathe   (watch a tick) | ./install.sh up (start stack)";;
  deps)     deps "${2:-}";;
  initdb)   initdb;;
  up)       up;;
  down)     down;;
  breathe)  breathe;;
  doctor)   doctor;;
  *) die "unknown subcommand '$1' (scaffold|deps|initdb|up|down|breathe|doctor)";;
esac
