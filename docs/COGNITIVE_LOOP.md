# Jarvis Cognitive Loop — Architecture (v4 DRAFT)

> Status: **DRAFT for review.** This is the core reasoning loop of Jarvis. It is meant to be
> durable, model-agnostic, skeptical when it matters, data-first for live claims, and
> self-learning by construction.
> Nothing here is built yet — this is the spec we agree on *before* writing code.

---

## 1. Why this exists

Today Jarvis has two divergent flows (the chat `_chat_reply` and the autonomous kernel `tick`),
neither of which is reliably skeptical or self-verifying. The concrete failure that motivated this:
a cached "WhatsApp fix not deployed" note was **replayed as fact** and was wrong — the loop never
re-checked reality. The cognitive loop makes the good behaviors **structural**: lightweight for daily
use, mandatory for risky/live work, and always followed by classified learning.

### Design goals → made structural

| Requirement | How it is guaranteed |
|---|---|
| durable | Loop is a persisted **Task Record** state machine; every stage checkpoints; every external call is bounded; a restart resumes or safely finalizes |
| model-agnostic | One `Brain.turn()` contract; adapters hide native-tools vs text-recovery |
| self-learns by nature | **LEARN** runs after both daily and heavy paths; learning is always allowed, but trust is never automatic |
| check existing docs | **RECALL** stage |
| be skeptical / wonder if docs are true | **APPRAISE** stage — claims are hypotheses, distrust by default |
| data-first by nature | **GROUND** stage — verify claims against reality, not the doc |
| gather enough info | **SUFFICIENCY** gate |
| do the actual work | **ACT** stage |
| wonder if the work was correct | **VERIFY** stage — self-check against reality (no agent spawning) |
| self-learn / update notes | **LEARN** stage writes verified, provenance-tagged notes; retires false ones |

---

## 2. The loop profiles

```
Daily:
FRAME → RECALL(light) → ACT(tool loop if needed) → LEARN(candidate notes)

Heavy:
FRAME → RECALL → APPRAISE → GROUND ⇄ SUFFICIENCY → ACT → VERIFY → LEARN(verified notes)
                    ▲                      │                    │
                    └─ claim disproven /   │   verify fails →   │
                       not enough ─────────┘   re-ground/act ───┘
```

One harness, two execution profiles. Jarvis defaults to the **daily** path because it must be usable
as a daily driver. It escalates to the **heavy** path when stale or unverified information could cause
damage.

The loop is driven by two triggers:
- **chat** — an owner message.
- **tick** — the autonomous kernel perceiving work.

Chat is low latency and can ask the owner when genuinely blocked. Tick is slower, must not over-ask,
and should escalate more readily because no owner is actively steering it.

### Stage 0 — FRAME
Capture the task (owner message *or* perceived work) **and a one-line acceptance criterion**
("what would make this answer/change correct?"). Pick an **execution profile**:

- **daily** (normal conversation / low-risk work): light recall, simple model/tool loop, auto-learn
  candidate context after the turn.
- **heavy** (mutating / live-state / high-stakes): full loop + stricter VERIFY (re-inspect reality after
  acting) + `deploy`/`infra_mutate` gating.

Default to daily. Escalate to heavy using deterministic rules first, model judgment second.

Force **heavy** if any of these are true:
- the task will change files, data, services, providers, tickets, deploys, schedules, or infrastructure,
- the answer depends on live state: deployed, running, broken, fixed, queue empty, latest, current config,
  current logs, or current provider/account state,
- the user asks to debug, verify, deploy, fix, restart, monitor, create a ticket, open a PR, or run
  automation,
- the task touches money, secrets, customer data, security, production, legal, medical, or provider
  accounts,
- recalled memory is volatile, stale, low-confidence, or contradicted,
- the same task already failed once, or gathered evidence conflicts,
- the trigger is an autonomous tick and an action may be taken without the owner present.

The selector may escalate mid-turn. A daily turn becomes heavy if Jarvis discovers it needs live
production state, is about to mutate something, or sees contradictions.

### Stage 1 — RECALL
Pull relevant notes / docs / memory for the task. Load them as **hints** carrying provenance,
freshness, confidence, volatility, and trust policy — **never as facts**.

### Stage 2 — APPRAISE  *(skepticism by default)*
For each recalled hint, decide trust. Mark **MUST-VERIFY** if the hint is:
- **volatile** (deploy state, runtime/service status, prices, "X is broken/working"), or
- **stale** (past its TTL / `last_verified_at` too old), or
- **low-confidence**, or
- **contradicted** by anything else recalled.

Default stance for volatile operational claims: prove it. Memory is a lead for what to check, not an
answer to repeat.

### Stage 3 — GROUND  *(data-first by nature)*
Gather evidence from **reality** via tools, **verification-first**: confirm the must-verify claims
against the live artifact / runtime / API / DB / filesystem — not the document that asserted them.
(This is the structural cure for stale-replay: deploy state is *volatile*, so it gets re-checked,
never trusted from a note.)

### Stage 4 — SUFFICIENCY
"Do I have enough, and is it trustworthy?" → **ACT** / gather more (back to GROUND) / **ask the owner**
(only if genuinely blocked). Bounded by a step budget so a tick stays inside its lock and chat stays
responsive.

### Stage 5 — ACT
Produce the answer, or execute the change via tools/workers. Gated by `mode` + `action_classes`
(propose-only unless allowed). This is where the actual work happens.

### Stage 6 — VERIFY  *(self-check against reality — no agent spawning)*
"Was the work correct?" Check the **outcome against reality and the acceptance criterion**:
- re-inspect the artifact, re-run the test/command, confirm the live state actually changed.
- for **heavy** tier, one extra **skeptical re-read pass** (same model, fresh framing:
  "from this evidence alone, try to disprove the result").

VERIFY checks **reality, not its own reasoning** — that is what keeps a same-model self-check useful.
If VERIFY fails, loop back (GROUND/ACT) with the disconfirming evidence.

### Stage 7 — LEARN  *(auto-learning, always on)*
Write/upgrade notes after both daily and heavy paths. Auto-learning is mandatory, but memory is
classified so future turns know how skeptical to be.

Daily mode can learn:
- user preferences,
- stable project facts,
- repo paths and recurring workflows,
- durable summaries of useful conversations,
- docs or files inspected as starting points.

Heavy mode can learn stronger records because it verified reality:
- root causes proven,
- deployed commits confirmed,
- tests or smoke checks passed,
- service or live URL state observed,
- tickets or PRs created,
- false memories retired or corrected.

Guards against laundering errors into "facts":
- the default `trust_policy` is **use_as_hint**,
- confidence **decays** over time,
- contradictions **block** promotion to high confidence,
- `verified` records the **method + time** ("verified by `ssh ... grep` at 2026-06-16T08:00"),
  never "true forever."

This feeds RECALL/APPRAISE on the next loop. Learning is allowed aggressively; trusting is conservative.

---

## 3. The Note model (the linchpin)

Stale-knowledge replay is cured by giving every note metadata instead of storing bare answers:

```text
Note {
  claim             # the assertion, in one line
  kind              # preference | procedure | stable_fact | snapshot | volatile_status | warning | gap
  sources[]         # where it came from (file, command, owner, url, tool, model_summary)
  evidence_ref      # pointer to proof; avoid storing raw logs or secrets
  last_observed_at  # when Jarvis saw this
  last_verified_at  # when reality last confirmed it, if ever
  created_at
  confidence        # low | medium | high; decays with age
  volatility        # stable | snapshot | volatile
  ttl               # how long before it must be re-verified
  trust_policy      # use_as_hint | can_use_directly | must_verify_before_answer
}
```

APPRAISE re-verifies any note that is **volatile, past TTL, low-confidence, contradicted, or marked
`must_verify_before_answer`**.
The current knowledge files already carry `verified=` and `_answered <ts>` frontmatter — this schema
extends that rather than inventing new storage.

Default `trust_policy` is **use_as_hint**.

Use **can_use_directly** only for narrow durable context:
- user preferences,
- stable personal instructions,
- stable repo conventions,
- durable procedures that are not dangerous if slightly stale.

Use **must_verify_before_answer** for anything live or operational:
- deployed state,
- service status,
- queues,
- provider/account status,
- "fixed" / "broken" claims,
- prices, dates, schedules, and anything current.

**Volatility examples**
- *stable*: "the dashboard is a single-file vanilla-JS app", "lc-backend deploys from `main`".
- *volatile*: "the WhatsApp fix is deployed", "service X is running", "the queue is empty".

> Note: volatility classification is itself a model judgment and can be wrong. Mis-labeling a volatile
> fact as stable is the main way stale knowledge could still slip through. Treat the classifier as
> fallible and let VERIFY/LEARN correct it.

---

## 4. Provider-agnostic Brain contract

```
Brain.turn(messages, tools) -> Turn { text, tool_calls[], thinking }
```

- HTTP backends (deepseek / openai / anthropic) → native structured `tool_calls`.
- CLI backends (claude / codex) → text output normalized through the recovery layer.

The loop never branches on provider; it just gets `text` and/or `tool_calls`. Wraps the existing
`llm` router.

---

## 5. Durability

- The loop is a **Task Record** persisted with its current stage + per-stage checkpoints.
- On restart: resume in-progress work, or **safely finalize** it (no frozen state).
- Every external call (model, tool, subprocess) is **time-bounded**.
- Builds directly on the durability fixes already shipped: CLI timeout watchdog,
  crash-finalize in the reply path, and the startup reaper for orphaned streams.
- Keep the state machine **minimal** — checkpoints, not a heavyweight workflow engine. (More
  machinery for correctness = more surface area to break; resist over-building.)

---

## 6. Comparison to what we have today

| Concern | Today | v4 |
|---|---|---|
| Loops | Two divergent: rich `_chat_reply` vs single-call, stubbed `tick` | One harness with daily and heavy execution profiles |
| Skepticism | None — `fast_local_answer`/knowledge replay cached conclusions as fact | APPRAISE distrusts; volatile/stale notes must be re-verified |
| Data-first | A deterministic pre-pass exists, but the model can still answer from docs/memory | GROUND mandatory for heavy/live claims |
| Verify own work | Not in the chat loop | VERIFY self-check against reality in heavy mode |
| Learning | `reflect_and_learn` background, keyword-triggered, no freshness | LEARN runs after both profiles; trust policy keeps memories as hints |
| Durability | Freeze fixes patched ad hoc | Task Record checkpoints make heavy work structural |
| Provider-agnostic | Native tools only on HTTP; CLI scrapes text | Uniform `Brain.turn` |
| Evidence phases | Three overlapping (planned probe + pre-loop + answer loop) | Daily path stays small; heavy path collapses evidence into GROUND ⇄ SUFFICIENCY |
| Cost control | None | Daily by default; heavy only when rules escalate |

---

## 7. Reuse vs build

**Reuse**
- the `llm` router (provider abstraction),
- `chat_tools` + the native-tool loop as the daily tool loop and heavy GROUND layer,
- knowledge files (extend frontmatter to the full Note schema),
- `perceive` / `workers.runner` (tick trigger + gated execution),
- the durability fixes already shipped (timeout / reaper / finalize).

**Build new**
- the daily/heavy selector with hard-rule escalation,
- the Task Record state machine,
- APPRAISE (note trust / freshness),
- the Note schema upgrade (provenance, decay, contradiction checks),
- VERIFY (self-check against reality),
- the `Brain.turn` wrapper.

---

## 8. Build plan (incremental, behind the current flow)

1. **Note schema + trust policy** — make every learned record a hint by default. Highest correctness
   ROI, lowest risk.
2. **APPRAISE/freshness for recalled memory** — re-check volatile/stale/contradicted hints before use.
3. **Daily/heavy selector** — deterministic hard rules first, model judgment only for escalation.
4. **`Brain.turn` contract** — unify provider handling over the existing router.
5. **Daily chat path** — simple model/tool loop plus post-turn LEARN.
6. **Heavy Task Record state machine** — full GROUND/SUFFICIENCY/ACT/VERIFY for risky work.
7. **Point the autonomous tick at the same selector** — with a heavier default budget profile.

Each step must stand alone and be reversible.

---

## 9. Known weaknesses / honest limits

This architecture **reduces** confirmation bias and stale knowledge; it **cannot eliminate** them,
because its own verification and volatility judgments are made by the same fallible models.

1. **Ceremony cost.** Skepticism + grounding + verify on every message would be slow/expensive — hence
   the daily/heavy split. But selection itself can mis-judge and under-verify something that mattered.
2. **Self-check has correlated blind spots.** Without an independent agent, a confidently-wrong model
   can rubber-stamp itself. Mitigated by making VERIFY check **reality, not its own reasoning** — but
   not fully solved. (An opt-in spawned verifier could be added later for a specific high-stakes area.)
3. **Institutionalizing confident errors.** If VERIFY is fooled, LEARN may write a high-confidence
   "verified" note that APPRAISE will later trust too much. Mitigated by defaulting to `use_as_hint`,
   confidence decay, contradiction checks, and `verified = method + time` — never absolute.
4. **Volatility classifier is fallible.** Mis-labeling volatile as stable is the main remaining path
   for stale knowledge to slip through.
5. **Not everything is falsifiable.** VERIFY works for "is it deployed? does the test pass?"; for
   judgment/creative work there is no artifact to inspect and self-check is just another opinion.
6. **Acceptance criteria are load-bearing.** Turning a vague request into a good checkable criterion is
   itself a fallible reasoning step that VERIFY depends on.

Build it acknowledging (1)–(6): daily by default, heavy when risk demands it, and notes that never become absolute truth. Built that
way it is a real improvement. Built assuming verification = truth, it will be confidently wrong in new,
harder-to-spot ways.

---

*Decisions to tweak before commit: selector hard rules, default TTLs per volatility class, the exact
Note storage format, memory redaction/evidence-reference rules, and where (if anywhere) an opt-in
independent verifier is worth re-introducing.*
