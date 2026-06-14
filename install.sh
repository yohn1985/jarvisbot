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

  gen config.example.yaml <<'EOF'
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

  gen jarvis/__init__.py <<'EOF'
"""Jarvis: a standalone self-improving agent runtime."""
__version__ = "0.0.1"
EOF

  gen jarvis/config.py <<'EOF'
"""Config loader: DEFAULTS <- config.example.yaml (documented base) <- config.yaml (your
deltas) <- .env (secrets). Deep-merged, so your config.yaml stays tiny. Degrades gracefully
(runs on DEFAULTS alone if pyyaml is absent)."""
from pathlib import Path

DEFAULTS = {
    "identity": {"name": "Jarvis", "mode": "shadow"},
    "priorities": [   # young Jarvis: self + learning first; production backlog (p3) last
        "p0_active_incident", "p1_unfinished_wip", "p2_self_caused_regression",
        "p4_self_maintenance", "p5_curiosity", "p3_needs_human_backlog",
    ],
    "action_classes": {"investigate": "allow", "propose": "allow", "fix_pr": "ask",
                       "deploy": "deny", "infra_mutate": "deny", "self_modify": "deny"},
}

def _merge(base: dict, over: dict) -> dict:
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base

def load(root: str | None = None) -> dict:
    import os
    root = Path(root or Path(__file__).resolve().parent.parent)
    cfg = dict(DEFAULTS)
    try:
        import yaml  # optional; shadow mode runs without it
    except Exception:
        return cfg
    extras = Path(os.environ.get("JARVIS_EXTRAS", root / "extras"))
    # precedence: DEFAULTS <- example (generic) <- config.yaml (local) <- extras/config.yaml (private overlay)
    for p in (root / "config.example.yaml", root / "config.yaml", extras / "config.yaml"):
        if p.exists():
            try:
                _merge(cfg, yaml.safe_load(p.read_text()) or {})
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
    return decision

def act(cfg, decision, world):
    """Route the decided rung to a worker — Jarvis's hands. The runner enforces propose-only:
    it only executes when mode!=shadow AND the worker's action_class is 'allow'; otherwise it
    records the INTENT. So this is always safe to call."""
    rung = decision["rung"]
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
    try:
        from jarvis.adapters.llm import build_llm
        from jarvis import messaging
    except Exception:
        return
    llm = build_llm(cfg)
    if not llm:
        return
    recurring = [r.get("sig") for r in world.get("_ledger", {}).get("recurring", [])][:3]
    prompt = (
        f"You are {cfg['identity']['name']}, an autonomous ops agent, on a {decision['mode']} tick.\n"
        f"You triaged to rung={decision['rung']} -> action: {decision['action']}.\n"
        f"Open backlog items: {world.get('_backlog_count', 0)}. Recurring issues in memory: {recurring}.\n\n"
        "In 2-3 sentences, say whether this is the right next move and the concrete first step.\n"
        "If you genuinely cannot proceed safely without information only the owner has, INSTEAD reply with "
        "exactly one line starting 'QUESTION: ' followed by your question."
    )
    try:
        out = llm.run("triage", prompt, timeout=120).strip()
    except Exception as e:
        decision["thought"] = f"(no LLM: {str(e)[:80]})"
        return
    decision["model"] = (cfg.get("llm", {}).get("routing", {}) or {}).get("triage", "")
    if out.upper().startswith("QUESTION:"):
        q = out.split(":", 1)[1].strip()
        decision["asked"] = q
        decision["thought"] = f"stuck -> asked owner: {q}"
        try:
            messaging.post_question(q, ref=decision["action"][:60])   # dashboard (canonical)
        except Exception:
            pass
        try:                                                          # + Telegram if configured
            from jarvis.adapters.notifier import TelegramNotifier
            tg = TelegramNotifier()
            if tg.enabled:
                tg.ask(q)
        except Exception:
            pass
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
EOF

  seed jarvis/adapters/__init__.py <<'EOF'
"""Pluggable adapters: nothing infra-specific lives in the kernel core."""
EOF
  gen jarvis/adapters/llm.py <<'EOF'
"""LLM provider router (the mind): role -> "backend:model", dispatched to a CLI backend.
Generalizes ai-exec. Backends are CLI command templates in config ({model}/{prompt}; no
{prompt} placeholder => prompt is piped on stdin). A routed backend that's absent or fails
falls through to `fallbacks`. Stdlib only."""
from __future__ import annotations
import shutil, subprocess

DEFAULT_BACKENDS = {
    "claude": ["claude", "-p", "--model", "{model}"],
    "codex":  ["codex", "exec", "--model", "{model}", "{prompt}"],
    "ollama": ["ollama", "run", "{model}", "{prompt}"],
}

class RoutingLLM:
    def __init__(self, routing, backends, aliases, fallbacks):
        self.routing = routing or {}
        self.backends = backends or DEFAULT_BACKENDS
        self.aliases = aliases or {}
        self.fallbacks = fallbacks or []

    def _resolve(self, target):
        backend, _, model = (target or "").partition(":")
        return backend, self.aliases.get(model, model)

    def _present(self, backend):
        cmd = self.backends.get(backend)
        return bool(cmd) and shutil.which(cmd[0]) is not None

    def _invoke(self, backend, model, prompt, timeout):
        cmd = [a.replace("{model}", model) for a in self.backends[backend]]
        stdin = None
        if any("{prompt}" in a for a in cmd):
            cmd = [a.replace("{prompt}", prompt) for a in cmd]
        else:
            stdin = prompt
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "nonzero").strip()[:200])
        return p.stdout.strip()

    def run(self, role, prompt, timeout=120):
        targets, tried = [], []
        if self.routing.get(role):
            targets.append(self.routing[role])
        targets += [f for f in self.fallbacks if f not in targets]
        for target in targets:
            backend, model = self._resolve(target)
            if not self._present(backend):
                tried.append(f"{backend}:absent"); continue
            try:
                return self._invoke(backend, model, prompt, timeout)
            except Exception as e:
                tried.append(f"{backend}:{str(e)[:50]}")
        raise RuntimeError(f"no usable LLM backend for role '{role}' (tried: {tried})")

def build_llm(cfg):
    llm = cfg.get("llm") or {}
    if not llm:
        return None
    return RoutingLLM(llm.get("routing"), llm.get("backends"),
                      llm.get("aliases"), llm.get("fallbacks"))
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
  seed jarvis/adapters/notifier.py <<'EOF'
"""Notifier adapter (placeholder; real impl ships as a tracked file)."""
from abc import ABC, abstractmethod
class Notifier(ABC):
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
  seed jarvis/safety/seatbelt.py <<'EOF'
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
  gen jarvis/skills.py <<'EOF'
#!/usr/bin/env python3
"""Skill registry: discover skills by scanning skills/*/SKILL.md frontmatter.
A skill is a self-contained folder (manifest + code + own requirements), so the
skills/ tree is a modular, extensible repo — drop in a folder, it's a new capability.
The mind lists skills (cheap), reads a SKILL.md just-in-time, and invokes the entrypoint."""
import os, sys, re, json
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
# Skills come from the core skills/ AND the private overlay extras/skills/ (operator add-ons).
SKILL_DIRS = [ROOT / "skills", Path(os.environ.get("JARVIS_EXTRAS", ROOT / "extras")) / "skills"]

def _frontmatter(md: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---", md, re.S); fm = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1); fm[k.strip()] = v.strip()
    return fm

def list_skills() -> list:
    out = []
    for base in SKILL_DIRS:
        for d in sorted(base.glob("*/SKILL.md")):
            fm = _frontmatter(d.read_text()); fm["dir"] = str(d.parent)
            fm.setdefault("name", d.parent.name); out.append(fm)
    return out

if __name__ == "__main__":
    if sys.argv[1:2] == ["list"]:
        for s in list_skills():
            print(f"  {s.get('name'):22} {s.get('description','')}")
    else:
        print(json.dumps(list_skills(), indent=2))
EOF
  gen skills/README.md <<'EOF'
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

migrate(){
  [ -f "$ROOT/.env" ] && set -a && . "$ROOT/.env" && set +a || true
  initdb
  log "importing the loopback ledger -> ai_memory.episodes ..."
  "$(pybin)" -c "import sys;sys.path.insert(0,'$ROOT');from jarvis.config import load;from jarvis.memory.store import build_store;s=build_store(load());n=s.migrate_from_jsonl();print('  backend:',s.backend,'| imported',n,'new episodes' if n>=0 else '| no postgres connection — memory uses the jsonl ledger (fine)')"
}

up(){   [ -f "$ROOT/.env" ] || { cp "$ROOT/.env.example" "$ROOT/.env"; warn "created .env from example — set real secrets!"; }
        docker compose -f "$ROOT/docker-compose.yml" up -d && log "stack up (postgres + redis)"; }
down(){ docker compose -f "$ROOT/docker-compose.yml" down && log "stack down"; }

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
  log "dashboard:"; ss -ltnp 2>/dev/null | grep -q ':8787' && log "  up on :8787" || warn "  not running (./install.sh dashboard)"
  log "memory backends:"; "$(pybin)" -c "import sys;sys.path.insert(0,'$ROOT');from jarvis.config import load;from jarvis.memory.store import build_store;from jarvis.memory.working import build_working;c=load();print('  episodic:',build_store(c).backend,'| working:',build_working(c).backend)" 2>/dev/null
}

# ---------------------------------------------------------------------------
dash_url(){ local ip; ip="$(hostname -I 2>/dev/null | awk '{print $1}')"; echo "http://${ip:-127.0.0.1}:8787"; }

# Bring the dashboard up (stdlib only — works before any deps/brain exist) so the owner has a
# place to watch Jarvis and answer its setup questions. Backgrounded + detached; the durable
# systemd service is installed later via the approved plan.
dashboard_up(){
  mkdir -p "$ROOT/state"
  if ss -ltn 2>/dev/null | grep -q ':8787 '; then log "dashboard already up"; return; fi
  setsid nohup "$(pybin)" "$ROOT/jarvis/dashboard/server.py" --host 0.0.0.0 --port 8787 \
    >"$ROOT/state/dashboard.log" 2>&1 </dev/null &
  echo $! > "$ROOT/state/dashboard.pid"
  sleep 1
  if ss -ltn 2>/dev/null | grep -q ':8787 '; then log "dashboard started (pid $(cat "$ROOT/state/dashboard.pid"))"
  else warn "dashboard may not have started — see $ROOT/state/dashboard.log"; fi
}

# The landing step: bring the cockpit up, run one no-AI preflight so the page shows what's
# missing, then hand the owner the URL to continue setup from the dashboard.
land(){
  dashboard_up
  "$(pybin)" -c "import sys;sys.path.insert(0,'$ROOT');from jarvis.config import load;from jarvis.bootstrap import preflight;r=preflight.run(load());print('[jarvis] preflight: ready=%s asked=%s'%(r['ready'],r['asked']))" 2>/dev/null || true
  echo
  log "Jarvis is up. Continue in your browser:"
  printf '\n    \033[36m%s\033[0m\n\n' "$(dash_url)"
  log "Open that page to finish setup — approve installs and give Jarvis its AI brain."
}

# Make Jarvis durable: a dedicated least-privilege 'jarvis' user, scoped (or yolo) sudoers, the
# install relocated to a jarvis-owned dir, and systemd units that restart + start on boot.
# Privileged — run as root (interactive first-run), NOT from the dashboard (it stops the loop/dash).
install_service(){
  [ "$(id -u)" -eq 0 ] || die "install-service must run as root (sudo ./install.sh install-service)"
  local U=jarvis DIR="${JARVIS_SERVICE_DIR:-/opt/jarvis}" MODE="${JARVIS_SUDO:-scoped}"
  command -v systemctl >/dev/null 2>&1 || die "no systemd (systemctl) on this host"
  log "install-service: user=$U dir=$DIR sudo=$MODE"
  id "$U" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$DIR" --shell /usr/sbin/nologin "$U"
  mkdir -p "$DIR"
  if [ "$ROOT" != "$DIR" ]; then
    [ -f "$ROOT/state/dashboard.pid" ] && kill "$(cat "$ROOT/state/dashboard.pid")" 2>/dev/null || true
    cp -a "$ROOT"/. "$DIR"/ && rm -rf "$DIR/.git"
  fi
  chown -R "$U":"$U" "$DIR"
  # scoped sudoers = only what the agent legitimately needs unattended; yolo = full root (opt-in).
  if [ "$MODE" = yolo ]; then
    echo "jarvis ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/jarvis
  else
    cat > /etc/sudoers.d/jarvis <<'SUD'
jarvis ALL=(root) NOPASSWD: /usr/bin/apt-get update, /usr/bin/apt-get install *, /usr/bin/systemctl daemon-reload, /usr/bin/systemctl start jarvis-*, /usr/bin/systemctl stop jarvis-*, /usr/bin/systemctl restart jarvis-*, /usr/bin/systemctl enable jarvis-*, /usr/bin/systemctl disable jarvis-*
SUD
  fi
  chmod 0440 /etc/sudoers.d/jarvis
  visudo -cf /etc/sudoers.d/jarvis >/dev/null || { rm -f /etc/sudoers.d/jarvis; die "generated sudoers invalid"; }
  cat > /etc/systemd/system/jarvis-loop.service <<EOF
[Unit]
Description=Jarvis wake loop
After=network-online.target
[Service]
Type=simple
User=$U
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
WorkingDirectory=$DIR
ExecStart=$DIR/install.sh dashboard --host 0.0.0.0 --port 8787
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now jarvis-loop.service jarvis-dashboard.service
  log "service installed + enabled (survives reboot). dashboard: $(dash_url)"
}

case "${1:-scaffold}" in
  scaffold) scaffold; echo; deps; echo; land;;
  setup|land) land;;
  install-service|service) install_service;;
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
  doctor)   doctor;;
  *) die "unknown subcommand '$1' (scaffold|deps|initdb|up|down|breathe|doctor)";;
esac
