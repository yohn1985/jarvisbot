# Reasoning controls & context windows — data-first research

Goal: fix the broken per-pool/per-model recognition of reasoning controls (effort vs thinking)
and context windows. Findings were verified live against each provider on the deployment host.

## Method

Queried live where a credential exists, used the CLIs' own `--help` + smoke tests where not:
- `ollama_cloud` — live: `GET /v1/models`, `POST /api/show` (per-model capabilities + context_length),
  and a real `POST /v1/chat/completions` to see if `reasoning_effort` is honored.
- `claude` CLI 2.1.174 — `claude --help`, plus smoke tests of `--effort` and `--betas`.
- `codex` CLI 0.139.0 — `codex --help`, `codex exec --help`.
- Anthropic / OpenAI HTTP APIs — attempted; **both keys are empty** (see below).

## Credential reality (this changes everything)

`/opt/jarvis/.env`: `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` are **empty (0 chars)**. `OLLAMA_API_KEY`
is set and works. So:
- claude & codex run via **subscription CLIs**, not API keys → **no live model/metadata listing**;
  capability + context data MUST come from a static map (or the CLI itself).
- ollama_cloud has a working key → **live** model list + `/api/show` metadata is the source of truth.

## Ground truth per pool

### claude (CLI `claude -p`, v2.1.174)
- Reasoning control = **`--effort`**, options **`low, medium, high, xhigh, max`** (native flag).
  - Smoke test: `claude -p --model claude-haiku-4-5 --effort low` → works (exit 0).
  - There is **no `--think`/`--thinking` flag** in this version.
- Models: aliases `opus|sonnet|haiku|fable` or full names (`claude-opus-4-8`, `claude-sonnet-4-6`,
  `claude-haiku-4-5`, `claude-fable-5`).
- Context window: **200K standard, with a 1M variant** for `sonnet`/`opus`/`fable` (NOT haiku),
  selected by a **`[1m]` model suffix** (e.g. `claude-sonnet-4-6[1m]`, `sonnet[1m]`). Verified the CLI
  accepts it; using it needs **usage credits** enabled on the account (`sonnet[1m]` without credits
  returns *"API Error: Usage credits required for 1M context · turn on usage credits at
  claude.ai/settings/usage"*). NOTE: the `--betas context-1m-2025-08-07` path is separate and IS
  API-key-only ("Custom betas are only available for API key users") — that is NOT how the CLI does
  1M. The model `[1m]` suffix is the correct mechanism.

### codex (CLI `codex exec`, v0.139.0)
- Reasoning control = **`model_reasoning_effort`** via `-c model_reasoning_effort=<v>` (already used).
  Values (OpenAI gpt‑5 family): **`minimal, low, medium, high`**. (Also `model_verbosity`,
  `model_reasoning_summary` exist as config knobs — not wired today.)
- Models: `gpt-5.5`, `gpt-5.5-codex`, `gpt-5`, `gpt-5-codex`, `gpt-5.3-codex`, `o4-mini`, … via `-m`.
- Context window: gpt‑5.x ≈ **400K** (static knowledge; no live source without a key).
- Note: codex effort option set is **different** from claude's (`minimal` vs `xhigh/max`).

### ollama_cloud (HTTP `https://ollama.com/v1`, key present)
- 35 models live. Per-model truth via `/api/show`:
  | model | capabilities | context_length |
  |---|---|---|
  | deepseek-v4-pro | completion, tools, **thinking** | 524288 (512K) |
  | deepseek-v4-flash | completion, tools, **thinking** | 1048576 (1M) |
  | gpt-oss:120b | completion, tools, **thinking** | 131072 (128K) |
  | glm-5 | **thinking**, completion, tools | 202752 (~198K) |
  | qwen3-coder:480b | completion, tools (**no thinking**) | 262144 (256K) |
- Reasoning control = **`reasoning_effort`** (`low|medium|high`). Honored for `thinking`-capable
  models (produces `reasoning`/`reasoning_content`); **silently ignored** (no error) for others.
  So "offer effort only when caps include `thinking`" is the right rule.

## Current code vs reality (the bugs)

Files: `jarvis/dashboard/server.py` (`_pool_control_caps`, `_model_controls`, `_context_window`,
`_STATIC_CTX`, option lists) and `jarvis/adapters/llm.py` (`_THINK_KEYWORD`, effort/thinking apply).

1. **claude mislabeled as thinking-only, no effort.** `_pool_control_caps` returns
   `{effort:False, thinking:True}` for claude. Reality: claude CLI has **`--effort`**, no thinking
   flag. The UI shows a "thinking" dropdown that the backend can't honor.
2. **claude uses a keyword hack instead of the real flag.** `_THINK_KEYWORD` appends
   "Ultrathink about this." to the prompt. Should pass **`--effort <level>`** to the CLI.
3. **Effort option sets are not per-pool.** One shared `_CODEX_EFFORT_OPTIONS`
   (`minimal/low/medium/high`) is used everywhere. Claude needs `low/medium/high/xhigh/max`;
   codex needs `minimal/low/medium/high`; ollama needs `low/medium/high`.
4. **Context windows hardcoded & stale; the 1M variant was missing.** `_STATIC_CTX` pins claude to
   200K, but sonnet/opus/fable also have a **1M variant via the `[1m]` model suffix** (usage-credit
   gated) — now offered as a 200K/1M picker that routes to `model[1m]` when 1M is chosen, with a
   graceful fall back to 200K if credits aren't enabled. (Earlier draft wrongly said 1M needed an API
   key — that's only the `--betas` path, not the CLI's `[1m]` suffix.)
5. **The control depends on BACKEND, not just pool/model.** The same Claude model is **effort** via
   the CLI but **thinking (budget_tokens)** via the anthropic HTTP API. `_pool_control_caps` keys off
   pool/format but the CLI-vs-API distinction for claude isn't modeled (CLI is treated as "thinking").
6. **ollama effort options include `none`** (`_OLLAMA_EFFORT_OPTIONS`) which isn't a real
   `reasoning_effort` value; should be `low/medium/high` (+ "default").
7. **No live capability source is used for claude/codex** (correct — no key), but the static map is
   thin and there's no per-model context-window override list.

## Proposed correct model (design for the fix — NOT yet implemented)

Make capability a function of **(backend_kind, model)**, resolved in one place:

- backend_kind ∈ { claude_cli, codex_cli, anthropic_http, openai_http, ollama_http, ollama_local }.
- Per backend_kind, a `reasoning` descriptor: `{control: "effort"|"thinking"|none, options: [...]}`:
  - claude_cli → effort: [low, medium, high, xhigh, max]
  - codex_cli / openai_http → effort: [minimal, low, medium, high]
  - anthropic_http → thinking: [low, medium, high, max] (budget_tokens)
  - ollama_http → effort: [low, medium, high] **iff** model caps include `thinking`, else none
- Context window: live (`/api/show`) for ollama; static map for claude/codex. Drop the unreachable
  1M option for the claude CLI (or gate it behind "anthropic_http + API key present").
- `adapters/llm.py` must apply the control the backend actually accepts:
  - claude_cli → `--effort <level>` (replace the keyword hack)
  - codex_cli → `-c model_reasoning_effort=<level>`
  - openai/ollama http → `reasoning_effort` in payload
  - anthropic_http → `thinking:{budget_tokens}`
- UI: the chip shows the *single* control the active backend+model supports, with the *correct*
  option set, and hides it when none. Context chip uses the resolved window.

## Open verification items
- codex: confirm whether the CLI also accepts `xhigh` (claude does; OpenAI API tops at `high`).
- ollama: confirm `reasoning_effort` value range per model family (some may accept only on/off).
- anthropic_http path is currently the only one modeling "thinking" (budget_tokens) — keep, but it's
  unused without an API key.
