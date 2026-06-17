<div align="center">

```
       ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗
       ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝
       ██║███████║██████╔╝██║   ██║██║███████╗
  ██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║
  ╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║
   ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝
```

**A self-hosted autonomous agent that onboards like a new hire: it lands on a machine, learns its
environment, and stays propose-only until you trust it with real work.**

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-3fae5a.svg)](LICENSE)
![Status](https://img.shields.io/badge/status-BETA-d8a13a.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-3fae5a.svg)
![Dependencies](https://img.shields.io/badge/core-stdlib%20only-3fae5a.svg)

[Website](https://jarvisbot.app) · [Wiki](https://github.com/yohn1985/jarvisbot/wiki)

<br>

![Jarvis — the welcome screen and guided setup](docs/welcome.png)

</div>

> ⚠️ **Beta.** Jarvis can read, reason, and act on a real machine. Run it on a box you
> trust, keep it in the default **shadow / propose-only** mode until you've watched it, and read
> the Safety section before granting it more.

---

## What it is

Jarvis is a persistent agent runtime built around one idea: a bounded loop with a real mind that you
can actually trust over time. Every wake it rebuilds its own context from durable memory, perceives
its world, decides **one** thing to do, optionally does it, and writes back what it learned, then it
sleeps until the next wake.

When it lands on a machine it **onboards like a new hire.** It pure-Python bootstraps itself onto a
fresh Linux box, learns its environment (services, network, how things connect), keeps a knowledge
base, and talks to you through a web dashboard. It stays **propose-only** while it is still learning.

It is **skeptical by design.** It doesn't trust a conclusion until that conclusion has survived an
adversarial red-team, and it routes what it can't verify to you instead of guessing.

## Quickstart

```bash
git clone https://github.com/yohn1985/jarvisbot.git
cd jarvisbot
./install.sh up            # venv, deps, services, dashboard
```

The installer prints your **login link** (it embeds a private access token):

```
http://<this-host>:8787/?token=…
```

Open it. Lost it? Run `./install.sh url` any time (or `./install.sh token` for the raw token).
Then connect a brain in **Settings → Brain & Models** (a Claude/ChatGPT subscription login is the
cheapest; an API key works too) and say hello.

## How it works

```
wake → perceive → orient → decide (priority ladder) → act → reflect
```

- **`jarvis/kernel.py`** — one bounded tick. Dumb, reliable plumbing around a single "mind" call.
- **`jarvis/perceive.py`** — builds the world from durable signals (a source *outage* is surfaced,
  not silently treated as "nothing to do").
- **`jarvis/adapters/llm.py`** — a model router: `role → backend:model`. CLI backends (Claude,
  Codex, Ollama) and HTTP backends (OpenAI-compatible + Anthropic). It detects each model's real
  controls and exposes only those: **reasoning effort** (`--effort` for the Claude CLI,
  `reasoning_effort` for Codex/OpenAI/Ollama, `thinking` budget for the Anthropic API) and the
  **context window** — including the Claude **200K / 1M** variant and per-model `num_ctx` for Ollama.
- **`jarvis/mcp.py`** — a provider-agnostic **MCP** host. Add Model Context Protocol servers (local
  stdio or remote HTTP with OAuth 2.1) and their tools are offered to **whatever brain is answering** —
  HTTP backends get them as native tool-calls; the Claude CLI gets them via its own `--mcp-config`.
- **`jarvis/memory/`** — working memory (Redis, degrades to in-process) + episodic memory
  (Postgres, degrades to a JSONL ledger). Reflections are written **and read back**, so experience
  changes behavior.
- **`jarvis/verify.py`** — the deep-fix discipline distilled: spawn a red-team agent to *disprove* a
  claim; **fail-closed** (an un-run check never counts as "passed").
- **`jarvis/safety/seatbelt.py`** — the self-modification **seatbelt**: snapshot → tamper-proof
  fitness gate → independent red-team → one-command rollback. Built and tested, but **nothing drives
  it autonomously yet** — the harness is ready for when self-editing lands. `self_modify` off by default.
- **`skills/`** — shipped capabilities (web research, network discovery, deep code search,
  youtube-research). Install deps with one click; run from chat with `/skill …`. Add your own from
  the dashboard (paste a `SKILL.md`) or just ask Jarvis to write one — they're auto-detected.

## The dashboard

A zero-dependency (stdlib) web UI:

- **Chat** — streaming replies token-by-token, a collapsible **Thought** block, markdown + syntax-
  highlighted code, **inline images** (vision in, and Jarvis can post images out), steering (a new
  message interrupts the current one), and a Stop button. Type `/` to run a skill. Per-conversation
  chips let you switch **model**, **effort/thinking**, and **context window** (e.g. Claude 200K↔1M)
  inline, without leaving the chat.
- **Knowledge** — what Jarvis has discovered and organized about its world.
- **Settings** (in the sidebar) — **Brain & Models** (connect a brain; route each role to a
  `backend:model` with its detected effort + context controls and capability badges), **Behavior**
  (identity, action classes, priorities, operating prompt), **Skills** (install / add), **MCP**
  (add servers, one-click OAuth Connect), and **Activity** (kernel ticks, workers, live processes).

## Safety defaults (read this)

- **Propose-only out of the box.** Risky action classes (`deploy`, `infra_mutate`, `self_modify`)
  are **deny** by default; you grant them deliberately in Settings.
- **The mind never scores its own work** — the fitness harness is a separate, tamper-proof process.
- **Verification fails closed** — degraded checks return "not verified," never a false pass.
- **The access token is your password** — the dashboard is gated; keep your link private. Secrets
  live only in a gitignored `.env` (chmod 600) / your vault, never in source.

## Configuration

Copy `config.example.yaml` → `config.yaml` and edit (everything is also editable from the dashboard).
Secrets go in `.env` (see `.env.example`). The `jarvis/` package is the source of truth — edit files
directly; `install.sh` never overwrites them.

## Status

Beta and moving fast. The core loop, memory, multi-backend model router (with per-model effort +
context controls), MCP support, the dashboard, skills, and the self-modification **seatbelt** are in
place. (The seatbelt is the safety harness — Jarvis does **not** autonomously edit its own code yet.)
Expect rough edges; issues and PRs welcome.

## Roadmap

Today Jarvis onboards and assists in propose-only mode. The direction is a **roles** system: once it
knows your environment well enough, you teach it a job by showing it, and it grows into owning that
responsibility one trust-gated step at a time, judged on outcomes rather than handed over wholesale.

The roles are **yours to define.** Whatever you would hand a capable junior is fair game (watching an
alert stream, tending a pipeline, taking first pass at a class of tickets, whatever your work is).
This is the plan, not a shipped feature yet. The current foundation (propose-only defaults,
fail-closed verification, action-class gating, the self-mod seatbelt) exists precisely so capability
can be granted carefully instead of blindly.

## Contributing

This is open source because the vision is bigger than one person. If you see it, help build it —
open an issue or a PR. See the [Wiki](https://github.com/yohn1985/jarvisbot/wiki) for architecture
deep-dives and how to add adapters/skills.

## License

[Apache License 2.0](LICENSE) · Home: [jarvisbot.app](https://jarvisbot.app)
