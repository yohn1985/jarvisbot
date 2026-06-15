#!/usr/bin/env bash
# ============================================================================
#  Jarvis — install.sh   (bootstrap + run; the jarvis/ package is the source of truth)
# ----------------------------------------------------------------------------
#  A standalone, open-source-clean, self-improving agent runtime.
#  This installer SEEDS missing project files (never overwrites your code), sets up the
#  stack, brings up the dashboard, and lets the shadow kernel "breathe".
#  To change Jarvis: edit the files under jarvis/ directly.
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
PORT="${JARVIS_PORT:-8787}"
# Prompts read the REAL terminal (curl|bash makes stdin the pipe, not the keyboard). No tty,
# or JARVIS_NONINTERACTIVE=1 => silent: take defaults / JARVIS_* env overrides.
TTY=""; { [ "${JARVIS_NONINTERACTIVE:-0}" != 1 ] && [ -r /dev/tty ]; } && TTY=/dev/tty
log(){ printf '\033[36m[jarvis]\033[0m %s\n' "$*"; }
warn(){ printf '\033[33m[jarvis] WARN:\033[0m %s\n' "$*"; }
die(){ printf '\033[31m[jarvis] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# write a GENERATED file (always refreshed from this script). $1=relpath, stdin=content.
gen(){ local p="$ROOT/$1"; mkdir -p "$(dirname "$p")"; cat > "$p"; log "wrote $1"; }
# write a file ONLY if absent (never clobber user data/secrets). $1=relpath, stdin=content.
seed(){ local p="$ROOT/$1"; [ -e "$p" ] && { log "kept  $1 (exists)"; return; }; mkdir -p "$(dirname "$p")"; cat > "$p"; log "seeded $1"; }

# ---------------------------------------------------------------------------
scaffold(){
  # The jarvis/ package is the SOURCE OF TRUTH; this installer no longer embeds it.
  [ -f "$ROOT/jarvis/kernel.py" ] || die "run from a clone of the repo (github.com/yohn1985/jarvisbot) — the jarvis/ package must be present"
  log "materializing Jarvis skeleton under $ROOT"

  seed README.md <<'EOF'
# Jarvis

A standalone, self-improving agent runtime: it wakes on a loop, perceives its
world from durable memory, decides ONE bounded thing to do, spawns workers,
audits itself, and writes back what it learned. A "sleeper" that rebuilds its
context every wake; a swarm coordinated through shared memory.

**The `jarvis/` package is the source of truth — edit the files directly.** `install.sh`
bootstraps and runs everything; it only *seeds* the package when files are missing, so re-running
it never overwrites your code.

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
EOF

  seed LICENSE <<'EOF'
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
state/
config.yaml
deploy/
extras/
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

  seed config.example.yaml <<'EOF'
# Jarvis config — NON-secret, environment-specific. Copy to config.yaml and edit.
identity:
  name: Jarvis
  owner: you              # your name or handle (used in prompts/messages)
  mode: shadow            # shadow | assist | autonomous  (start in shadow!)

# What may it DO without asking? Everything else is propose-only.
action_classes:
  investigate: allow      # read-only: always safe
  propose: allow          # write a plan / open a PR (reviewed)
  fix_pr: ask             # open a fix PR through the red-team gate
  deploy: deny            # irreversible / customer-facing → earn it
  infra_mutate: deny      # nginx/dns/firewall → never autonomous yet
  self_modify: deny       # edit its own code → only via the seatbelt, opt-in deliberately

# LLM = a router over a model catalog (claude / codex / ollama local+cloud).
llm:
  think_on_tick: true       # let the tick reason via the LLM (and ask when stuck)
  routing:                  # role -> "backend:model" (cost/capability tiered)
    triage:        claude:haiku         # the wake-tick decision can be cheap
    orchestrator:  claude:opus          # deep judgment — frontier only
    red_team:      claude:opus          # self-audit must be sharp
    fixer:         codex:gpt-5.5        # code edits
    researcher:    claude:sonnet        # high-volume leaf work
    summarizer:    claude:haiku         # log/area summarization — cheapest
  fallbacks: [claude:opus]              # used when the routed backend is absent/fails
  aliases:                  # short name -> real model id
    opus: claude-opus-4-8
    sonnet: claude-sonnet-4-6
    haiku: claude-haiku-4-5
  backends:                 # CLI templates ({model},{prompt}; no {prompt} => prompt on stdin)
    claude: ["claude","-p","--model","{model}"]
    codex:  ["codex","exec","--model","{model}","{prompt}"]
    # ollama: disabled for now (no local ollama on this box; qwen weak). When re-enabling,
    # deepseek-v4 is the strong pick: ollama: ["ollama","run","{model}","{prompt}"]

memory:
  working:   {kind: redis,    url: redis://localhost:6379/0}
  episodic:  {kind: postgres, dsn: "postgresql://jarvis:${POSTGRES_PASSWORD}@localhost:5432/ai_memory"}
  # episodic may also set  ledger: /path/to/external.jsonl  (read fallback when postgres is down)
  semantic:  {kind: none}    # none | mem0 | zep   (add when needed)

notifier:
  kind: telegram             # telegram | slack | stdout
  casual: true               # talk like a colleague, not a dashboard

worksource:
  kind: folder               # folder | gitea | github
  path: ./tasks              # for 'folder'
  # for 'gitea', set these in config.yaml (NEVER commit your infra into this example):
  #   api: http://your-gitea:3000/api/v1
  #   repo: owner/findings-repo
  #   token_cmd: /path/to/script-that-prints-a-gitea-token.sh

# Jarvis's hands. Each worker = a command template ({target}) + the action_class it needs.
# Generic no-op examples here; override in config.yaml with your real tools (deep-fix, fixer).
# Jarvis only EXECUTES when mode!=shadow AND the action_class is 'allow'; else it records intent.
workers:
  deep_fix: {cmd: ["echo","[deep-fix] would investigate {target}"], action_class: propose}
  fixer:    {cmd: ["echo","[fixer] would open a PR for {target}"], action_class: fix_pr}

# Wake triggers
wake:
  heartbeat_seconds: 1800    # floor so it never sleeps forever
  alert_webhook: true        # Alertmanager -> poke
  self_schedule: true        # it sets its own next-wake

# The priority ladder the tick triages by (top non-empty rung wins).
# Young Jarvis works on ITSELF and LEARNS first; it only takes on the production backlog (p3)
# once it understands its world and has earned trust. Raise p3 up the ladder as it matures.
priorities:
  - p0_active_incident         # a fire is always first
  - p1_unfinished_wip          # never abandon what I started
  - p2_self_caused_regression  # if I broke something, fix it
  - p4_self_maintenance        # improve my own tooling / process
  - p5_curiosity               # learn about my world
  - p3_needs_human_backlog     # only then work on the system
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

  seed tasks/.keep <<'EOF'
# Drop task files here for the 'folder' worksource adapter.
EOF

  gen deploy/jarvis.service <<EOF
[Unit]
Description=Jarvis wake loop
After=network-online.target
[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=$ROOT/install.sh run
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF
  gen deploy/jarvis-dashboard.service <<EOF
[Unit]
Description=Jarvis dashboard
After=network-online.target
[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=$ROOT/install.sh dashboard --host 0.0.0.0 --port 8787
Restart=always
[Install]
WantedBy=multi-user.target
EOF
  scaffold_skills
  log "scaffold complete."
}

# Skills FRAMEWORK lives in the installer; individual skills are modular folders
# under skills/<name>/ (manifest + code + own requirements) — an extensible repo.
scaffold_skills(){
  seed skills/README.md <<'EOF'
# Jarvis skills

A modular, extensible capability repo. Each skill is a self-contained folder:

```
skills/<name>/
  SKILL.md          # manifest (frontmatter) + when-to-use + usage
  skill.py          # entrypoint (CLI: python skill.py <args>)
  requirements.txt  # the skill's OWN deps (core stays lean)
```

`SKILL.md` frontmatter:
```
---
name: <name>
description: <one line — used by the mind to decide relevance>
entrypoint: skill.py
requires: <comma-separated pip deps>
when_to_use: <when the mind should reach for this>
---
```

Manage skills:
```
./install.sh skill list
./install.sh skill install <name>     # deps into the project .venv
./install.sh skill run <name> [args]
```

A skill should be runnable standalone (for testing) AND callable by the kernel.
EOF
}

# ---------------------------------------------------------------------------
deps(){
  local install="${1:-}"
  log "checking prerequisites..."
  local missing=()
  for c in python3; do command -v "$c" >/dev/null 2>&1 && log "  ok   $c" || { warn "  MISSING $c (required)"; missing+=("$c"); }; done
  # docker is OPTIONAL — without it Jarvis runs on the local jsonl ledger (no Postgres/Redis).
  if command -v docker >/dev/null 2>&1; then
    docker compose version >/dev/null 2>&1 && log "  ok   docker + compose (optional: Postgres/Redis)" || warn "  docker present but no 'compose' plugin (optional)"
  else
    warn "  no docker (optional) — Jarvis runs on the local jsonl ledger without it"
  fi
  for c in claude codex ollama; do have_cli "$c" && log "  ok   $c (LLM backend)" || warn "  missing $c CLI (optional backend)"; done
  [ -f "$ROOT/config.yaml" ] && log "  ok   config.yaml" || warn "  no config.yaml yet (copy from config.example.yaml)"
  [ -f "$ROOT/.env" ] && log "  ok   .env" || warn "  no .env yet (copy from .env.example, add secrets)"
  if [ "$install" = "--install" ] && [ "${#missing[@]}" -gt 0 ]; then
    warn "auto-install of system packages (${missing[*]}) is gated — run the printed commands yourself or approve explicitly."
  fi
}

initdb(){
  command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 || { warn "no docker — skipping Postgres schema (memory uses the jsonl ledger)"; return 0; }
  [ -f "$ROOT/.env" ] && set -a && . "$ROOT/.env" && set +a || true
  log "applying db/schema.sql to ai_memory..."
  docker compose -f "$ROOT/docker-compose.yml" exec -T postgres \
    psql -U jarvis -d ai_memory < "$ROOT/db/schema.sql" && log "schema applied." || warn "initdb failed (is the stack up?)"
}

migrate(){
  [ -f "$ROOT/.env" ] && set -a && . "$ROOT/.env" && set +a || true
  initdb
  log "importing the loopback ledger -> ai_memory.episodes ..."
  "$(pybin)" -c "import sys;sys.path.insert(0,'$ROOT');from jarvis.config import load;from jarvis.memory.store import build_store;s=build_store(load());n=s.migrate_from_jsonl();print('  backend:',s.backend,'| imported',n,'new episodes' if n>=0 else '| no postgres connection — memory uses the jsonl ledger (fine)')"
}

# ---- guided first-run: ask, suggest defaults, never block a non-interactive install ----------
ask(){ # ask "Question" "default" [ENVVAR]  -> echoes the answer
  local q="$1" def="$2" ev="${3:-}" ans=""
  [ -n "$ev" ] && [ -n "${!ev:-}" ] && { printf '%s' "${!ev}"; return; }
  [ -z "$TTY" ] && { printf '%s' "$def"; return; }
  printf '\033[36m? %s\033[0m [%s]: ' "$q" "$def" > "$TTY"
  IFS= read -r ans < "$TTY" || ans=""
  printf '%s' "${ans:-$def}"
}
ask_choice(){ # ask_choice "Question" defaultN ENVVAR opt1 opt2 ...  -> echoes the chosen option text
  local q="$1" defn="$2" ev="$3"; shift 3; local opts=("$@") n i=1 o
  [ -n "$ev" ] && [ -n "${!ev:-}" ] && { printf '%s' "${!ev}"; return; }
  [ -z "$TTY" ] && { printf '%s' "${opts[$((defn-1))]}"; return; }
  { printf '\033[36m? %s\033[0m\n' "$q"
    for o in "${opts[@]}"; do printf '   %d) %s%s\n' "$i" "$o" "$([ "$i" -eq "$defn" ] && printf '   [default]')"; i=$((i+1)); done
    printf '  choose [%d]: ' "$defn"; } > "$TTY"
  IFS= read -r n < "$TTY" || n=""; n="${n:-$defn}"
  case "$n" in ''|*[!0-9]*) n="$defn";; esac
  { [ "$n" -ge 1 ] && [ "$n" -le "${#opts[@]}" ]; } || n="$defn"
  printf '%s' "${opts[$((n-1))]}"
}

# Detect a CLI even when it isn't on the install-time PATH (curl|bash runs a non-login shell, so
# tools in ~/.local/bin, npm-global, snap, brew etc. can be invisible). Check PATH, the login PATH,
# and common install dirs.
have_cli(){
  command -v "$1" >/dev/null 2>&1 && return 0
  bash -lc "command -v $1" >/dev/null 2>&1 && return 0
  local d; for d in "$HOME/.local/bin" /usr/local/bin /snap/bin "$HOME/.ollama/bin" /home/linuxbrew/.linuxbrew/bin; do
    [ -x "$d/$1" ] && return 0
  done
  return 1
}

# Write the guided choices into config.yaml (string edits — no PyYAML needed at install time;
# config is only READ once PyYAML is present, so this safely takes effect when deps land).
apply_config(){
  [ -f "$ROOT/config.yaml" ] || return 0
  local fb; case "$G_BRAIN" in codex) fb="codex:gpt-5.5";; ollama) fb="ollama:llama3.1";; *) fb="claude:opus";; esac
  python3 - "$ROOT/config.yaml" "$G_OWNER" "$G_MODE" "$fb" <<'PY' 2>/dev/null || warn "couldn't patch config.yaml — edit owner/mode by hand"
import re,sys
p,owner,mode,fb=sys.argv[1:5]
s=open(p).read()
s=re.sub(r'(?m)^(  owner:)[^\n#]*', lambda m:m.group(1)+' '+owner+'  ', s, count=1)
s=re.sub(r'(?m)^(  mode:)\s*\S+',    lambda m:m.group(1)+' '+mode,       s, count=1)
s=re.sub(r'(?m)^(  fallbacks:)\s*\[[^\]]*\]', lambda m:m.group(1)+' ['+fb+']', s, count=1)
open(p,'w').write(s)
PY
  [ "$G_BRAIN" = ollama ] && warn "ollama brain: set your local model in config.yaml (llm.routing/aliases) or the dashboard (defaulted fallback to ollama:llama3.1)"
}

guided(){
  [ -f "$ROOT/config.yaml" ]  || { [ -f "$ROOT/config.example.yaml" ] && cp "$ROOT/config.example.yaml" "$ROOT/config.yaml"; }
  [ -f "$ROOT/.env" ]         || { cp "$ROOT/.env.example" "$ROOT/.env" 2>/dev/null; chmod 600 "$ROOT/.env" 2>/dev/null; warn "created .env from example — set real secrets there"; }
  if [ -n "$TTY" ]; then log "Jarvis setup — press Enter to accept each [default]."; else log "non-interactive — using defaults (override with JARVIS_* env vars)."; fi

  G_OWNER="$(ask 'Your name or handle' "${USER:-you}" JARVIS_OWNER)"

  # Default = the full-capability install. Jarvis is meant to DO things (install packages, manage
  # services); the behavioral brakes are autonomy mode (shadow) + action_classes, not crippled OS
  # access. Pick the foreground cockpit if you'd rather keep it rootless.
  local run; run="$(ask_choice 'How should Jarvis run?' 1 JARVIS_RUN 'durable system service (/opt/jarvis, full capability — recommended)' 'foreground cockpit (~/jarvisbot, no root)')"
  case "$run" in foreground*|cockpit|2) G_RUN=foreground;; *) G_RUN=service;; esac

  local m; m="$(ask_choice 'Autonomy level' 1 JARVIS_MODE 'shadow — propose-only (recommended)' 'assist' 'autonomous')"
  G_MODE="$(printf '%s' "$m" | awk '{print $1}')"

  local have_docker=0; command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 && have_docker=1
  local memdef=2; [ "$have_docker" -eq 1 ] && memdef=1
  local mem; mem="$(ask_choice 'Memory backend' "$memdef" JARVIS_MEMORY 'Docker Postgres + Redis (richer memory)' 'local jsonl ledger (no extra services)')"
  case "$mem" in Docker*|docker|1) G_MEM=docker;; *) G_MEM=ledger;; esac
  [ "$G_MEM" = docker ] && [ "$have_docker" -eq 0 ] && { warn "Docker unavailable — using the local jsonl ledger instead."; G_MEM=ledger; }

  local opts=(); have_cli claude && opts+=('claude (Claude CLI)'); have_cli codex && opts+=('codex (Codex CLI)'); have_cli ollama && opts+=('ollama (local)'); opts+=('set up later in the dashboard')
  local brain; brain="$(ask_choice 'Main AI brain' 1 JARVIS_BRAIN "${opts[@]}")"
  case "$brain" in claude*) G_BRAIN=claude;; codex*) G_BRAIN=codex;; ollama*) G_BRAIN=ollama;; *) G_BRAIN=later;; esac

  G_SUDO=scoped; G_PORT="$PORT"
  if [ "$G_RUN" = service ]; then
    local s; s="$(ask_choice 'sudo access for the jarvis user' 1 JARVIS_SUDO 'full root (recommended — Jarvis needs it to do real work)' 'scoped — apt-get + systemctl only')"
    case "$s" in scoped*|2) G_SUDO=scoped;; *) G_SUDO=yolo;; esac
    G_PORT="$(ask 'Dashboard port' "$PORT" JARVIS_PORT)"; PORT="$G_PORT"
  fi

  apply_config
  printf '\033[36m[jarvis]\033[0m chosen: owner=%s · run=%s · mode=%s · memory=%s · brain=%s%s\n' \
    "$G_OWNER" "$G_RUN" "$G_MODE" "$G_MEM" "$G_BRAIN" "$([ "$G_RUN" = service ] && printf ' · sudo=%s · port=%s' "$G_SUDO" "$G_PORT")"
}

# 'up' = the guided bring-up the bootstrap calls. Must never exit non-zero on a box without
# docker (the bootstrap runs it under `set -e`) — that was the early-beta install failure.
up(){
  guided
  if [ "$G_RUN" = service ]; then
    if [ "$(id -u)" -eq 0 ]; then JARVIS_SUDO="$G_SUDO" JARVIS_PORT="$G_PORT" install_service
    else
      log "durable service needs root — re-running just the service step with sudo…"
      sudo -E JARVIS_NONINTERACTIVE=1 JARVIS_SUDO="$G_SUDO" JARVIS_PORT="$G_PORT" \
        JARVIS_SERVICE_DIR="${JARVIS_SERVICE_DIR:-/opt/jarvis}" "$ROOT/install.sh" install-service \
        || warn "service install failed — retry later with: sudo ./install.sh install-service"
    fi
    return 0
  fi
  if [ "$G_MEM" = docker ]; then
    docker compose -f "$ROOT/docker-compose.yml" up -d && log "stack up (postgres + redis)" || warn "docker stack failed to start — continuing on the local jsonl ledger"
  else
    log "memory: local jsonl ledger (no Docker stack started)"
  fi
  land
}
down(){ command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 || { warn "no docker — nothing to bring down"; return 0; }
        docker compose -f "$ROOT/docker-compose.yml" down && log "stack down"; }

pybin(){ [ -x "$ROOT/.venv/bin/python" ] && echo "$ROOT/.venv/bin/python" || echo python3; }
breathe(){ log "one tick:"; "$(pybin)" "$ROOT/jarvis/kernel.py"; }

venv(){ [ -d "$ROOT/.venv" ] || python3 -m venv "$ROOT/.venv" >/dev/null 2>&1; echo "$ROOT/.venv"; }
skill_cmd(){
  local cmd="${1:-list}"; shift 2>/dev/null || true
  case "$cmd" in
    list)    python3 "$ROOT/jarvis/skills.py" list;;
    install) local n="${1:-}"; [ -n "$n" ] || die "usage: skill install <name>"
             [ -f "$ROOT/skills/$n/requirements.txt" ] || die "no such skill: $n"
             local v; v="$(venv)"; "$v/bin/pip" install -q -r "$ROOT/skills/$n/requirements.txt" \
               && log "installed deps for skill '$n' into .venv";;
    run)     local n="${1:-}"; shift 2>/dev/null || true; [ -n "$n" ] || die "usage: skill run <name> [args]"
             local py="$ROOT/.venv/bin/python"; [ -x "$py" ] || py=python3
             "$py" "$ROOT/skills/$n/skill.py" "$@";;
    *) die "usage: skill (list | install <name> | run <name> [args])";;
  esac
}

doctor(){
  deps; echo
  log "fitness (tamper-proof self-check):"; "$(pybin)" "$ROOT/jarvis/safety/fitness.py" 2>/dev/null | sed 's/^/  /'
  log "dashboard:"; ss -ltnp 2>/dev/null | grep -q ":${PORT}" && log "  up on :${PORT}" || warn "  not running (./install.sh dashboard)"
  log "memory backends:"; "$(pybin)" -c "import sys;sys.path.insert(0,'$ROOT');from jarvis.config import load;from jarvis.memory.store import build_store;from jarvis.memory.working import build_working;c=load();print('  episodic:',build_store(c).backend,'| working:',build_working(c).backend)" 2>/dev/null
}

# ---------------------------------------------------------------------------
dash_token(){ local f="$ROOT/state/dashboard_token"; mkdir -p "$ROOT/state"
  [ -s "$f" ] || { python3 -c "import secrets;print(secrets.token_urlsafe(24))" > "$f"; chmod 600 "$f" 2>/dev/null; }
  cat "$f"; }
dash_url(){ local ip; ip="$(hostname -I 2>/dev/null | awk '{print $1}')"; echo "http://${ip:-127.0.0.1}:${PORT}/?token=$(dash_token)"; }

# A loud, hard-to-miss login banner — printed at the very END of setup so the URL isn't lost in scroll.
login_banner(){ # $1 = login url, $2 = dir (for the re-print hint)
  local url="$1" dir="${2:-$ROOT}" bar='══════════════════════════════════════════════════════════════════'
  printf '\n\033[32m%s\033[0m\n' "$bar"
  printf '  \033[1;32m✓  JARVIS IS READY — open this link to log in:\033[0m\n\n'
  printf '      \033[1;36m%s\033[0m\n\n' "$url"
  printf '  The link contains your private access token — keep it private.\n'
  printf '  Re-print it any time:  \033[2mcd %s && ./install.sh url\033[0m\n' "$dir"
  printf '  Repo: github.com/yohn1985/jarvisbot   ·   Docs: jarvisbot.app\n'
  printf '\033[32m%s\033[0m\n\n' "$bar"
}

# Bring the dashboard up (stdlib only — works before any deps/brain exist) so the owner has a
# place to watch Jarvis and answer its setup questions. Backgrounded + detached; the durable
# systemd service is installed later via the approved plan.
dashboard_up(){
  mkdir -p "$ROOT/state"
  if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then log "dashboard already up"; return; fi
  setsid nohup "$(pybin)" "$ROOT/jarvis/dashboard/server.py" --host 0.0.0.0 --port "$PORT" \
    >"$ROOT/state/dashboard.log" 2>&1 </dev/null &
  echo $! > "$ROOT/state/dashboard.pid"
  sleep 1
  if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then log "dashboard started (pid $(cat "$ROOT/state/dashboard.pid"))"
  else warn "dashboard may not have started — see $ROOT/state/dashboard.log"; fi
}

# The landing step: bring the cockpit up, run one no-AI preflight so the page shows what's
# missing, then hand the owner the URL to continue setup from the dashboard.
land(){
  dashboard_up
  "$(pybin)" -c "import sys;sys.path.insert(0,'$ROOT');from jarvis.config import load;from jarvis.bootstrap import preflight;r=preflight.run(load());print('[jarvis] preflight: ready=%s asked=%s'%(r['ready'],r['asked']))" 2>/dev/null || true
  echo
  log "Finish setup in the dashboard — give Jarvis its AI brain."
  login_banner "$(dash_url)" "$ROOT"
}

# Make Jarvis durable: a dedicated least-privilege 'jarvis' user, scoped (or yolo) sudoers, the
# install relocated to a jarvis-owned dir, and systemd units that restart + start on boot.
# Privileged — run as root (interactive first-run), NOT from the dashboard (it stops the loop/dash).
install_service(){
  [ "$(id -u)" -eq 0 ] || die "install-service must run as root (sudo ./install.sh install-service)"
  # Run as the OWNER (the human who installed it), NOT a locked-down 'jarvis' user — so the service
  # inherits their AI-CLI binaries (~/.local/bin) AND their CLI login/auth (~/.claude, ~/.codex).
  # A separate user can read neither (auth files are 0600, owner-only), so the brain never connects.
  local U="${JARVIS_USER:-${SUDO_USER:-$(logname 2>/dev/null)}}"
  local DIR="${JARVIS_SERVICE_DIR:-/opt/jarvis}" MODE="${JARVIS_SUDO:-yolo}"
  { [ -n "$U" ] && id "$U" >/dev/null 2>&1; } || die "couldn't determine the owner user — re-run as: sudo JARVIS_USER=<you> ./install.sh install-service"
  command -v systemctl >/dev/null 2>&1 || die "no systemd (systemctl) on this host"
  local UHOME; UHOME="$(getent passwd "$U" | cut -d: -f6)"; UHOME="${UHOME:-/home/$U}"
  local SVC_PATH="$UHOME/.local/bin:$UHOME/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"
  log "install-service: user=$U dir=$DIR sudo=$MODE"
  # 1) sudoers FIRST + validated, before disrupting anything. sudoers forbids wildcards in command
  # ARGS, so scoped = the whole apt-get/systemctl binaries (still far better than full root);
  # yolo opt-in = full root. (Hardened single-helper-script scoping is a future improvement.)
  if [ "$MODE" = yolo ]; then
    echo "$U ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/jarvis
  else
    printf '%s ALL=(root) NOPASSWD: /usr/bin/apt-get, /usr/bin/systemctl\n' "$U" > /etc/sudoers.d/jarvis
  fi
  chmod 0440 /etc/sudoers.d/jarvis
  visudo -cf /etc/sudoers.d/jarvis >/dev/null || { rm -f /etc/sudoers.d/jarvis; die "generated sudoers invalid"; }
  # generate the dashboard access token BEFORE relocating, so /opt/jarvis ends up with the SAME
  # token the URL below prints. Otherwise the service starts, finds no token, generates its own,
  # and the login link we printed is rejected (the early-beta "new url token won't log in" bug).
  dash_token >/dev/null 2>&1 || true
  # 2) relocate to the install dir, owned by the owner user
  mkdir -p "$DIR"
  [ "$ROOT" != "$DIR" ] && { cp -a "$ROOT"/. "$DIR"/ && rm -rf "$DIR/.git"; }
  chown -R "$U" "$DIR"
  # 3) systemd units
  cat > /etc/systemd/system/jarvis-loop.service <<EOF
[Unit]
Description=Jarvis wake loop
After=network-online.target
[Service]
Type=simple
User=$U
Environment=PATH=$SVC_PATH
WorkingDirectory=$DIR
ExecStart=$DIR/install.sh run
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF
  cat > /etc/systemd/system/jarvis-dashboard.service <<EOF
[Unit]
Description=Jarvis dashboard
After=network-online.target
[Service]
Type=simple
User=$U
Environment=PATH=$SVC_PATH
WorkingDirectory=$DIR
ExecStart=$DIR/install.sh dashboard --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  # 4) hand :8787 from the landing dashboard to the durable service (kill the old one LAST)
  [ -f "$ROOT/state/dashboard.pid" ] && kill "$(cat "$ROOT/state/dashboard.pid")" 2>/dev/null || true
  pkill -f "dashboard/server.py" 2>/dev/null || true
  sleep 1
  systemctl enable --now jarvis-loop.service jarvis-dashboard.service
  log "service installed + enabled (survives reboot)."
  # build the URL from the SERVICE's own token (the one it actually validates), not $ROOT's.
  local ip dburl; ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  dburl="http://${ip:-127.0.0.1}:${PORT}/?token=$(cat "$DIR/state/dashboard_token" 2>/dev/null)"
  login_banner "$dburl" "$DIR"
}

# Tear Jarvis down: stop + remove the systemd services, the sudoers grant, and any legacy dedicated
# 'jarvis' system user. By default it KEEPS your data (state/ memory+token, .env secrets, extras/
# overlay) and tells you where it is; pass --purge to remove the install directory too.
uninstall(){
  local DIR="${JARVIS_SERVICE_DIR:-}"; [ -n "$DIR" ] || { [ -d /opt/jarvis ] && DIR=/opt/jarvis || DIR="$ROOT"; }
  local purge=0; [ "${1:-}" = "--purge" ] && purge=1
  [ -f "$ROOT/state/dashboard.pid" ] && kill "$(cat "$ROOT/state/dashboard.pid")" 2>/dev/null || true
  local has_sys=0
  if [ -e /etc/systemd/system/jarvis-loop.service ] || [ -e /etc/systemd/system/jarvis-dashboard.service ] || [ -e /etc/sudoers.d/jarvis ]; then has_sys=1; fi
  if [ "$has_sys" -eq 1 ] || [ "$purge" -eq 1 ]; then
    [ "$(id -u)" -eq 0 ] || die "removing the system service needs root — run: sudo ./install.sh uninstall (add --purge to remove data too)"
    systemctl disable --now jarvis-loop.service jarvis-dashboard.service 2>/dev/null || true
    rm -f /etc/systemd/system/jarvis-loop.service /etc/systemd/system/jarvis-dashboard.service
    systemctl daemon-reload 2>/dev/null || true
    pkill -f "jarvis/dashboard/server.py" 2>/dev/null || true
    rm -f /etc/sudoers.d/jarvis
    # remove ONLY a legacy dedicated 'jarvis' SYSTEM user (uid < 1000) — never a human/login user
    if id jarvis >/dev/null 2>&1 && [ "$(id -u jarvis 2>/dev/null)" -lt 1000 ]; then userdel jarvis 2>/dev/null || true; fi
    log "removed the systemd services, unit files, and sudoers grant."
  else
    log "no system service found — stopped any running dashboard."
  fi
  if [ "$purge" -eq 1 ]; then
    cd / 2>/dev/null || true; rm -rf "$DIR"
    log "purged $DIR — memory, secrets, and overlay all removed."
  else
    log "kept your data in $DIR (state/ memory+token, .env secrets, extras/ overlay)."
    log "to remove that too:  sudo ./install.sh uninstall --purge   (or  sudo rm -rf $DIR)"
  fi
  log "your AI-CLI logins (~/.claude, ~/.codex) are untouched."
}

case "${1:-up}" in
  scaffold) scaffold; echo; deps; echo; land;;
  setup|land) land;;
  install-service|service) install_service;;
  uninstall|remove) uninstall "${2:-}";;
  deps)     deps "${2:-}";;
  initdb)   initdb;;
  migrate)  migrate;;
  up)       up;;
  down)     down;;
  breathe)  breathe;;
  run)      shift; "$(pybin)" "$ROOT/jarvis/loop.py" "$@";;     # persistent wake loop
  wake)     mkdir -p "$ROOT/state"; touch "$ROOT/state/wake"; log "wake marker set";;
  selfcheck) "$(pybin)" "$ROOT/jarvis/safety/fitness.py";;     # tamper-proof fitness score
  archive)  "$(pybin)" -c "import sys;sys.path.insert(0,'$ROOT');from jarvis.safety.seatbelt import snapshot,list_archive;print('snapshot:',snapshot('manual'));print('archive:',list_archive()[:5])";;
  rollback) "$(pybin)" -c "import sys;sys.path.insert(0,'$ROOT');from jarvis.safety.seatbelt import rollback;print('rolled back' if rollback('${2:-}') else 'failed')";;
  skill)    shift; skill_cmd "$@";;
  dashboard) shift; "$(pybin)" "$ROOT/jarvis/dashboard/server.py" "$@";;
  url|login) login_banner "$(dash_url)" "$ROOT";;   # re-print the login link any time
  token)    cat "$ROOT/state/dashboard_token" 2>/dev/null || die "no token yet — run ./install.sh up";;
  doctor)   doctor;;
  *) die "unknown subcommand '$1' (up|down|breathe|run|url|token|skill|dashboard|doctor|install-service|uninstall [--purge])";;
esac
