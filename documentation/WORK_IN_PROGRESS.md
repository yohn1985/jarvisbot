
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
