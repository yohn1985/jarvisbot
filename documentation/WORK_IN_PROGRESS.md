
## 2026-06-17 - Restore real chat Thought and fix Gitea harness drift
**Files:** `jarvis/dashboard/dashboard.html`, `jarvis/adapters/llm.py`, `jarvis/harness.py`, `deploy.sh`, `tests/test_dashboard_login.py`, `tests/test_reasoning_controls.py`, `tests/test_chat_tool_recovery.py`
**Summary:** Restored the chat Thought block to real model reasoning only, instead of falling back to harness/tool evidence. Added OpenAI-compatible reasoning field parsing for `reasoning_content`, `reasoning`, `thinking`, and `thinking_content` so Ollama/GLM-style streams keep populating `m.thinking`. Made deterministic ticket/Gitea probes skip the extra model tool-decision round trip, and made deploy copy the private `extras/config.yaml` overlay to `/opt/jarvis/extras/config.yaml` when present so live Gitea config does not drift from the repo overlay.
**Verified:** `python3 -m unittest tests.test_reasoning_controls tests.test_chat_tool_recovery tests.test_dashboard_login tests.test_chat_observability`, `python3 -m unittest discover -s tests`, `python3 -m py_compile jarvis/*.py jarvis/**/*.py skills/*/skill.py`, and `git diff --check`.
**Deployed:** Commit `9e9ac86` deployed to `/opt/jarvis` with `./deploy.sh`; `jarvis-dashboard.service` and `jarvis-loop.service` were active with `NRestarts=0`. From `/tmp`, live `PYTHONPATH=/opt/jarvis python3 -m jarvis.gitea_tools open-summary 3` returned `total_open_issues=173 repos_checked=35`, proving the deployed private overlay is now loaded. Deployed `dashboard.html` contains `Thought is for real model reasoning only` and no `evidenceThought` fallback.
**Remaining:** Current live main brain is `codex:gpt-5.5`, which does not emit the same model-reasoning stream that Ollama/GLM did. Thought will be real when the selected backend emits reasoning; it will stay absent instead of showing fake tool evidence when the backend does not.

## 2026-06-18 - Chat Thought UI guardrail comments
**Files:** `jarvis/dashboard/dashboard.html`
**Summary:** Added code comments warning future tabs not to remove the `thinking`/`evidence` fallback that keeps the collapsible Thought block visible, and not to show the streaming progress/worklog box after completed messages.
**Verified:** `python3 -m unittest discover -s tests`, `python3 -m py_compile jarvis/*.py jarvis/**/*.py skills/*/skill.py`, and `git diff --check`.
**Deployed:** Commit `0679913` deployed to `/opt/jarvis`; `jarvis-dashboard.service` and `jarvis-loop.service` were active and deployed `dashboard.html` contained the guardrail comments.
**Remaining:** Superseded by the 2026-06-17 Thought separation above: the evidence fallback comment was intentionally replaced because Thought must mean real model reasoning, not tool evidence.

## 2026-06-18 - Hide completed chat progress box
**Files:** `jarvis/dashboard/dashboard.html`
**Summary:** Kept the streaming progress/worklog box only while Jarvis is actively working. Completed chat replies still have the collapsible Thought block, but no longer print the progress-step box after every answer.
**Verified:** `python3 -m unittest discover -s tests`, `python3 -m py_compile jarvis/*.py jarvis/**/*.py skills/*/skill.py`, and `git diff --check`.
**Remaining:** Deploy and refresh verification pending.

## 2026-06-18 - Restore chat Thought visibility
**Files:** `jarvis/dashboard/dashboard.html`
**Summary:** Restored the visible Thought block in chat replies. The harness was still storing the internal work trail in `evidence`, but the dashboard only rendered Thought from `thinking`, so replies with empty model-thinking appeared to have no Thought. The renderer now uses model `thinking` first and falls back to the harness `evidence` trail, while keeping completed-message worklog steps visible.
**Verified:** `python3 -m unittest discover -s tests`, `python3 -m py_compile jarvis/*.py jarvis/**/*.py skills/*/skill.py`, and `git diff --check`.
**Remaining:** Deploy and browser/live verification pending.

## 2026-06-18 - Idle tick skips main brain
**Files:** `jarvis/kernel.py`, `tests/test_curiosity_idle.py`
**Summary:** Stopped `idle` ticks from calling the orchestrator. Live verification after the self-maintenance cooldown showed Jarvis correctly reached `idle`, but then spent a Codex turn reflecting on idle state. Idle now records idle and waits for the next wake without model work.
**Verified:** `python3 -m unittest tests.test_curiosity_idle tests.test_chat_observability tests.test_dashboard_login`, `python3 -m unittest discover -s tests`, `python3 -m py_compile jarvis/*.py jarvis/**/*.py skills/*/skill.py`, and `git diff --check`.
**Correction:** Restored the run-feed `thought` text for `idle`, `p4_self_maintenance`, and `p5_curiosity` as deterministic status lines so the dashboard still explains what Jarvis is doing without launching a model for routine ticks.
**Remaining:** Deploy and live no-Codex-idle verification pending.

## 2026-06-18 - Self-maintenance loop cooldown
**Files:** `jarvis/perceive.py`, `jarvis/kernel.py`, `tests/test_curiosity_idle.py`
**Summary:** Fixed the next loop bottleneck after p5: recurring `tooling-gap` memory entries were promoted to `p4_self_maintenance` forever, but the kernel had no p4 action plan. Perception now suppresses self-maintenance items that already have an unanswered owner question or a recent p4/deep-fix run, and the kernel routes actionable p4 items through the bounded `deep_fix` worker without spending the main LLM on every heartbeat.
**Verified:** `python3 -m unittest tests.test_curiosity_idle tests.test_chat_observability tests.test_dashboard_login`, `python3 -m unittest discover -s tests`, `python3 -m py_compile jarvis/*.py jarvis/**/*.py skills/*/skill.py`, and `git diff --check`.
**Remaining:** Deploy and live perception verification pending.

## 2026-06-18 - Bounded p5 curiosity path
**Files:** `jarvis/kernel.py`, `tests/test_curiosity_idle.py`
**Summary:** Removed the remaining heavy harness path from autonomous `p5_curiosity` ticks. Stale discovery now always routes through the bounded discover skill, refreshes `state/explore.json`, and never starts a full Codex heavy task just because the environment snapshot is stale. `think()` now skips p5 curiosity entirely so routine discovery cannot spend the main brain before the bounded worker runs.
**Verified:** `python3 -m unittest tests.test_curiosity_idle tests.test_chat_observability tests.test_dashboard_login`, `python3 -m unittest discover -s tests`, `python3 -m py_compile jarvis/*.py jarvis/**/*.py skills/*/skill.py`, and `git diff --check`.
**Deployed:** Commit `654c275` deployed to `/opt/jarvis` with `./deploy.sh`; `jarvis-dashboard.service` and `jarvis-loop.service` were active, `/opt/jarvis/.deployed-version.json` showed `short_commit: 654c275`, and deployed `kernel.py` had no heavy harness branch for `p5_curiosity`.
**Live proof:** After deploy, no `codex exec` process was running from `/opt/jarvis`; Jarvis ran the bounded discover skill path instead, then the loop service remained active with `NRestarts=0`.
**Remaining:** None for the p5 heavy-loop fix. Current live perception also reports a separate `p4_self_maintenance` item (`deepfix-research-red-team-gap`) ahead of p5.

## 2026-06-18 - Curiosity idle guard
**Files:** `jarvis/perceive.py`, `jarvis/kernel.py`, `tests/test_curiosity_idle.py`
**Summary:** Stopped the autonomous loop from treating `environment` as permanently stale. Perception now returns curiosity work only when there are unanswered knowledge questions or the discovery interval has actually expired; otherwise the kernel returns `idle: no eligible work` instead of manufacturing a p5 curiosity task. This prevents repeated heavy `explore stalest area: environment` Codex churn when the queue is already clear.
**Verified:** `python3 -m unittest tests.test_curiosity_idle tests.test_chat_observability tests.test_dashboard_login`, `python3 -m unittest discover -s tests`, `python3 -m py_compile jarvis/*.py jarvis/**/*.py skills/*/skill.py`, and `git diff --check`.
**Remaining:** The separate ticket-33 password-login WIP remains intentionally unstaged.

## 2026-06-18 - Streaming progress and tool-loop guardrails
**Files:** `jarvis/messaging.py`, `jarvis/dashboard/server.py`, `jarvis/harness.py`, `jarvis/dashboard/dashboard.html`, `tests/test_chat_observability.py`
**Summary:** Tightened the chat observability path after a manual ticket-fix turn sat on generic `Working...` for minutes. Streaming messages now persist live `status`, `trace_id`, and `trace_path`; the chat UI can show `Working <duration>` plus a trace link before completion. The heavy tool-evidence loop now records `tool_decision_start` before slow model calls and stops after duplicate or consecutive failed model-chosen tools instead of grinding through the full loop.
**Verified:** `python3 -m unittest tests.test_chat_observability tests.test_chat_tool_recovery tests.test_dashboard_login`, `python3 -m unittest discover -s tests`, `python3 -m py_compile jarvis/*.py jarvis/**/*.py skills/*/skill.py`, and `git diff --check`.
**Remaining:** Separate ticket-33 password-login WIP remains intentionally unstaged.

## 2026-06-18 - Chat run timing and trace logging
**Files:** `jarvis/run_trace.py`, `jarvis/dashboard/server.py`, `jarvis/harness.py`, `jarvis/messaging.py`, `jarvis/dashboard/dashboard.html`, `tests/test_chat_observability.py`
**Summary:** Added durable per-reply JSONL traces under `state/chat_traces/`, chat message timing metadata, a `/api/chat-trace?id=<message_id>` endpoint, and a visible `Worked <duration>` footer with a trace button on completed Jarvis replies. Tool execution, profile choice, web decision, model stream/fallback, heavy verification, and finalization are now timed with sanitized event data.
**Verified:** `python3 -m unittest tests.test_chat_observability tests.test_chat_tool_recovery tests.test_dashboard_login`, `python3 -m unittest discover -s tests`, `python3 -m py_compile jarvis/*.py jarvis/**/*.py skills/*/skill.py`, `git diff --check`, and `./install.sh test`.
**Remaining:** Pre-existing password-login work was left unstaged and preserved separately; this change only ships chat/harness observability.

## 2026-06-18 - Gitea-agnostic ticket lookup and Codex routing repair
**Files:** `jarvis/harness.py`, `jarvis/gitea_tools.py`, `jarvis/adapters/llm.py`, `jarvis/chat_tools.py`, `jarvis/dashboard/server.py`, `tests/test_chat_tool_recovery.py`, `tests/test_reasoning_controls.py`, `tests/test_dashboard_login.py`
**Summary:** Fixed the Jarvis conversation failure where ticket `#34` was resolved from one hardcoded findings repo instead of the actual open `jarvis#34`. Ticket evidence now uses a config-driven Gitea helper that discovers repos at runtime and ranks open issues before closed issues and PRs. Dashboard Codex invocation now uses a canonical noninteractive command even if local config supplies a minimal or unsafe backend template. Screenshot URLs are handled as direct image inputs, and the login badge now says `BETA`.
**Verified:** `python3 -m unittest tests.test_chat_tool_recovery tests.test_reasoning_controls tests.test_dashboard_login`, `python3 -m unittest discover -s tests`, `python3 -m py_compile jarvis/*.py jarvis/**/*.py skills/*/skill.py`, `git diff --check`, `./install.sh test`, real Gitea probe `PYTHONPATH=<repo> python3 -m jarvis.gitea_tools issue 34`, and a live Codex CLI probe using the broken/minimal config shape returned `OK`.
**Deployed:** Commit `c7114f3` deployed to `/opt/jarvis` with `./deploy.sh`; `jarvis-dashboard.service` and `jarvis-loop.service` were active. `/opt/jarvis/.deployed-version.json` showed `short_commit: c7114f3`. Live `http://127.0.0.1:8787/` served `JARVIS <span class=beta>BETA</span>`. The deployed Gitea helper resolved `jarvis#34` first with `open_issue_matches=1`, and the deployed Codex routing probe returned `OK`.
**Closed:** Added deploy/test proof to `jarvis#34` and closed the issue.
**Remaining:** None for this incident.

## 2026-06-16 08:16 — auto-log
**Files:** COGNITIVE_LOOP.md
**Summary:** No summary captured.

## 2026-06-16 — Cognitive loop daily/heavy revision
**Files:** `docs/COGNITIVE_LOOP.md`
**Summary:** Updated the draft from v3 to v4. The spec now separates a fast daily-driver path from a heavy verification path, keeps auto-learning in both modes, and makes memory a typed hint with trust policy rather than truth.
**Verified:** Read the updated document back for consistency.
**Remaining:** Selector hard rules, TTL defaults, Note storage format, evidence redaction rules, and optional independent verifier policy still need design decisions before implementation.

## 2026-06-16 - Cognitive loop implemented and dashboard-tested
**Files:** `jarvis/harness.py`, `jarvis/local_knowledge.py`, `jarvis/dashboard/server.py`, `jarvis/kernel.py`, `tests/test_chat_tool_recovery.py`, `docs/COGNITIVE_LOOP.md`
**Summary:** Implemented the model-agnostic daily/heavy cognitive profile selector, typed learned-memory hints, trust metadata, stale/volatile memory handling, heavy-mode forced evidence gathering, and heavy-mode answer verification. Both daily and heavy paths still auto-learn, but learned notes are stored and retrieved as hints instead of facts.
**Verified:** `python3 -m unittest discover -s tests`, `git diff --check`, `python3 -m py_compile jarvis/*.py jarvis/**/*.py skills/*/skill.py`, and `./deploy.sh`.
**Frontend proof:** Tested through the running dashboard UI on `http://127.0.0.1:8787/`. Conversations:
- `chat-mqgnysnr`: Codex `codex:gpt-5.5`, daily profile, passed.
- `chat-mqgnztyi`: Codex `codex:gpt-5.5`, heavy profile, checked `jarvis-dashboard.service`, passed.
- `chat-mqgo0h3q`: Ollama Cloud `deepseek-v4-pro`, heavy profile, checked `jarvis-dashboard.service`, passed.
- `chat-mqgo10c9`: Claude `claude-opus-4-8`, daily profile, passed.
**Deployed:** Commit `f6603e6` deployed to `/opt/jarvis`; `jarvis-dashboard.service` and `jarvis-loop.service` were active after deploy and after frontend tests.
**Remaining:** The heavy verifier is intentionally conservative and may append a verification caveat when evidence is present but not framed in the exact shape the verifier expects. Future tuning can improve verifier prompts without changing the daily/heavy selector.

## 2026-06-16 - Heavy verifier evidence packing fix
**Files:** `jarvis/harness.py`, `tests/test_chat_tool_recovery.py`
**Summary:** Fixed the heavy-mode verifier prompt so it no longer sees only the head of long tool evidence. It now builds a verifier-focused evidence pack from matching command blocks plus the evidence tail.
**Verified:** `python3 -m unittest discover -s tests`, full Jarvis py-compile sweep, `git diff --check`, `./deploy.sh`.
**Frontend proof:** Retested through the running dashboard UI:
- `chat-mqgodyzx`: Codex `codex:gpt-5.5`, heavy profile, `jarvis-dashboard.service` verified with `heavy verification: passed`.
- `chat-mqgoem5f`: Ollama Cloud `deepseek-v4-pro`, heavy profile, `jarvis-dashboard.service` verified with `heavy verification: passed`.
**Deployed:** Commit `08f1db7` deployed to `/opt/jarvis`; model routing restored to Codex orchestrator, Claude verifier/researcher/summarizer, DeepSeek triage, Codex fixer.
**Remaining:** Follow-up heavy-task/tick wiring landed in commit `9832eb6` on `main`; it is not a hidden stash. Watch autonomous ticks for runtime behavior because the frontend retests exercised the chat path.

## 2026-06-16 - Model control detection cleanup
**Files:** `jarvis/dashboard/server.py`, `jarvis/dashboard/dashboard.html`, `tests/test_model_controls.py`
**Summary:** Reworked dashboard model controls so the selected pool is detected first, then model-specific capabilities decide whether effort or thinking is available. Ollama Cloud thinking models now expose effort with `none`, stale unsupported params are normalized out, and the composer/settings UI both read from `/api/model-info`.
**Verified:** `python3 -m unittest tests.test_model_controls`, `python3 -m unittest discover -s tests`, `python3 -m py_compile jarvis/dashboard/server.py tests/test_model_controls.py`, full Jarvis py-compile sweep, `git diff --check`, live `/api/model-info`, and Playwright through the running dashboard.
**Frontend proof:** Composer and Settings both show `ollama_cloud:deepseek-v4-pro` with effort options `default`, `none`, `low`, `medium`, `high`, no thinking dropdown, and detected context `524288`. Chat smoke `chat-mqgu4ra2` replied `4` through the frontend.
**Deployed:** Commit `7567bed` deployed to `/opt/jarvis`; `jarvis-dashboard.service` and `jarvis-loop.service` were active after deploy.
**Remaining:** None for model-control wiring. One generic browser resource 404 line appeared during Playwright, but no app/API failed responses were captured.

## 2026-06-18 - GLM reasoning-only chat recovery
**Files:** `jarvis/harness.py`, `jarvis/dashboard/server.py`, `tests/test_chat_tool_recovery.py`
**Summary:** Investigated a Jarvis dashboard chat where `ollama_cloud:glm-5.2` streamed useful reasoning but persisted `(no reply)`, and where a ticket-count question returned raw page-count command output instead of an answer. The provider itself returned normal `content` on direct GLM probes, so the fix keeps GLM and hardens Jarvis: ticket/count/Gitea and owner-correction turns now use the heavy evidence path, raw shell-output dumps are recovered into an answer, and reasoning/evidence-only turns force a final answer from gathered evidence before falling through.
**Verified:** `python3 -m unittest tests.test_chat_tool_recovery`, `python3 -m unittest discover -s tests`, `python3 -m py_compile jarvis/*.py jarvis/**/*.py skills/*/skill.py`, `git diff --check`, and `./install.sh test`.
**Frontend proof:** Direct Ollama Cloud probes confirmed GLM 5.2 returns `content` in stream/non-stream modes with tools. Exact conversation reproduction against the live state produced a real explanatory answer from GLM after evidence was available.
**Deployed:** Commit `d85464c` reached `/opt/jarvis`, but the deploy exposed a source/live unit-template drift: repo templates used nonexistent `User=jarvis`, so systemd restarted both Jarvis services with `status=217/USER`.
**Remaining:** Follow-up commit restored unit templates to `User=yohn`; `1c40617` deployed cleanly. Live API smoke proved the no-reply guard worked, but GLM selected weak evidence for ticket-count questions, so `2baab7d` added deterministic Gitea open-count and newest-open-issues probes for ticket/Gitea questions and deployed cleanly. Final live smoke through `/api/say` on `ollama_cloud:glm-5.2` returned a normal answer, included `total_open_issues` evidence, verified heavy mode, and did not persist `(no reply)`.
