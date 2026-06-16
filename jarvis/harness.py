"""Provider-neutral Jarvis chat harness.

The model is only the reasoning brain. Jarvis owns context loading, tool execution,
evidence accumulation, and the reflection pass that creates durable learning.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid as _uuid
from pathlib import Path

from jarvis import chat_tools, local_knowledge, persona

_JARVIS_ROOT = Path(__file__).resolve().parent.parent
_TASK_DIR = _JARVIS_ROOT / "state" / "tasks"


MAX_CONTEXT_BYTES = 18000
PROJECT_HINT_FILES = (
    "AGENTS.md",
    "CONTEXT.md",
    "PROJECT.md",
    "README.md",
    "WORK_IN_PROGRESS.md",
    "docs/INDEX.md",
    "documentation/INDEX.md",
)

_MUTATION_WORDS = (
    "fix", "change", "edit", "update", "write", "append", "create", "add",
    "implement", "patch", "save", "deploy", "restart", "run automation",
    "open pr", "create ticket", "file ticket",
)
_LIVE_STATE_WORDS = (
    "status", "current", "currently", "right now", "today", "latest", "recent",
    "running", "health", "stuck", "broken", "fixed", "deployed", "not deployed",
    "queue", "logs", "timer", "service", "pipeline", "worker", "host", "network",
    "storage", "uptime", "price", "version", "release",
)
_HIGH_RISK_WORDS = (
    "secret", "password", "token", "key", "customer", "production", "prod",
    "money", "billing", "payment", "legal", "medical", "provider", "account",
    "firewall", "dns", "nginx", "proxy", "database", "backup", "restore",
)


def _cfg_roots(cfg: dict) -> list[Path]:
    roots: list[Path] = []
    for env_name in ("JARVIS_CONTEXT_ROOT", "JARVIS_WORKSPACE"):
        raw = os.environ.get(env_name)
        if raw:
            roots.append(Path(raw).expanduser())
    for section, key in (
        ("workspace", "root"),
        ("context", "roots"),
        ("knowledge", "doc_roots"),
        ("documentation", "roots"),
    ):
        value = ((cfg or {}).get(section) or {}).get(key)
        if isinstance(value, str):
            roots.append(Path(value).expanduser())
        elif isinstance(value, list):
            roots.extend(Path(str(v)).expanduser() for v in value if str(v).strip())
    roots.extend(local_knowledge.remembered_roots())
    roots.append(chat_tools.default_cwd())
    out = []
    for p in roots:
        try:
            rp = p.resolve()
        except Exception:
            rp = p
        candidates = [rp]
        if rp.name.lower() in {"docs", "documentation"}:
            candidates.append(rp.parent)
        for candidate in candidates:
            if candidate.exists() and candidate not in out:
                out.append(candidate)
    return out[:12]


def _read_project_hints(cfg: dict) -> str:
    chunks = []
    for root in _cfg_roots(cfg):
        for rel in PROJECT_HINT_FILES:
            path = root / rel
            try:
                if path.exists() and path.is_file():
                    chunks.append(f"### {path}\n{path.read_text(errors='replace')[:6000]}")
            except Exception:
                continue
        if sum(len(c) for c in chunks) >= MAX_CONTEXT_BYTES:
            break
    return "\n\n".join(chunks)[:MAX_CONTEXT_BYTES]


def context_sources(cfg: dict) -> list[str]:
    """Visible grounding sources for the dashboard status line."""
    sources: list[str] = []
    for root in _cfg_roots(cfg):
        for rel in PROJECT_HINT_FILES:
            path = root / rel
            try:
                if path.exists() and path.is_file():
                    sources.append(str(path))
            except Exception:
                continue
        if len(sources) >= 8:
            break
    return sources[:8]


def build_context(cfg: dict, messages: list[dict], latest: str, env_context: str = "") -> dict:
    """Build the prompt base from the editable operating prompt plus discovered evidence."""
    remembered_roots = local_knowledge.remember_roots_from_text(latest, cfg)
    local_docs = local_knowledge.retrieve(latest, cfg)
    project_hints = _read_project_hints(cfg)
    try:                                          # skills catalog so the model auto-detects skills
        from jarvis import skills as _skills_mod
        skills_catalog = _skills_mod.catalog()
    except Exception:
        skills_catalog = ""
    transcript = "\n".join(
        _transcript_turn(m)
        for m in messages
        if m.get("kind") in ("message", "note", "answer", "question")
    )
    base = (
        persona.system(cfg)
        + "\n\nYou are chatting with the owner in the Jarvis dashboard."
        + (f"\n\nProject/context hints that exist on this installation:\n{project_hints}\n" if project_hints else "")
        + (f"\n\nWhat you have discovered about your environment:\n{env_context}\n" if env_context else "")
        + (f"\n\nLocal documentation evidence:\n{local_docs}\n" if local_docs else "")
        + (
            "\n\nSkills you can use — read the listed SKILL.md for full steps, then carry them out "
            "with your normal tools (e.g. run a referenced script via shell, then show_image its output):\n"
            + skills_catalog + "\n"
            if skills_catalog else ""
        )
        + (
            "\n\nThe owner gave you documentation path(s) that were durably remembered: "
            + ", ".join(remembered_roots)
            + "\n"
            if remembered_roots else ""
        )
        + "\n\nTool and evidence contract:\n"
        + "- Use available tools when local evidence is needed.\n"
        + f"- Current tool mode: {chat_tools.mode_description()}.\n"
        + "- Do not claim you ran, scanned, read, indexed, remembered, deployed, or checked anything unless evidence in this prompt proves it.\n"
        + "- Treat learned memories as hints. If a memory is volatile, stale, low-confidence, contradicted, or marked must_verify_before_answer, verify it against current evidence before answering it as true.\n"
        + "- Keep negative findings scoped to the evidence: say 'I found no matching X in Y' instead of 'there is no X' unless the evidence proves the universal claim.\n"
        + "- If evidence is missing, say what is missing and record the gap during reflection.\n"
        + "- If relevant learning gaps are included, say this is a known unresolved gap and name the next evidence needed. Do not answer as if the gap was never recorded.\n"
        + f"\nConversation so far:\n{transcript}\n"
    )
    return {
        "base": base,
        "local_docs": local_docs,
        "remembered_roots": remembered_roots,
        "project_hints": project_hints,
        "context_sources": context_sources(cfg),
    }


def select_execution_profile(latest: str, local_docs: str = "", trigger: str = "chat") -> dict:
    """Choose the daily or heavy chat path with deterministic escalation rules."""
    low = (latest or "").lower()
    reasons: list[str] = []
    required_checks: list[str] = []
    if trigger != "chat":
        reasons.append("autonomous trigger")
        required_checks.append("verify before acting")
    if _contains_any(low, _MUTATION_WORDS):
        reasons.append("mutating or work-producing request")
        required_checks.append("action permission")
    if _contains_any(low, _LIVE_STATE_WORDS):
        reasons.append("depends on live/current state")
        required_checks.append("live evidence")
    if _contains_any(low, _HIGH_RISK_WORDS):
        reasons.append("high-risk domain")
        required_checks.append("source/live evidence")
    memory_flags = _memory_trust_flags(local_docs)
    if memory_flags:
        reasons.extend(memory_flags)
        required_checks.append("memory freshness")
    mode = "heavy" if reasons else "daily"
    if mode == "daily":
        reasons = ["low-risk daily-driver request"]
    return {
        "mode": mode,
        "reason": "; ".join(dict.fromkeys(reasons)),
        "reasons": list(dict.fromkeys(reasons)),
        "required_checks": list(dict.fromkeys(required_checks)),
    }


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    for needle in needles:
        parts = re.findall(r"[a-z0-9]+", needle.lower())
        if not parts:
            continue
        pattern = r"\b" + r"\s+".join(re.escape(p) for p in parts) + r"\b"
        if re.search(pattern, text):
            return True
    return False


def _memory_trust_flags(local_docs: str) -> list[str]:
    text = local_docs or ""
    flags = []
    checks = (
        ("TRUST_POLICY: must_verify_before_answer", "recalled memory requires verification"),
        ("VOLATILITY: volatile", "recalled memory is volatile"),
        ("STALE: yes", "recalled memory is stale"),
        ("CONFIDENCE: low", "recalled memory is low-confidence"),
    )
    for needle, reason in checks:
        if needle in text:
            flags.append(reason)
    return flags


def profile_status(profile: dict) -> str:
    mode = (profile or {}).get("mode") or "daily"
    reason = (profile or {}).get("reason") or "low-risk daily-driver request"
    return f"cognitive profile: {mode} ({reason})"


def profile_prompt(profile: dict) -> str:
    mode = (profile or {}).get("mode") or "daily"
    checks = ", ".join((profile or {}).get("required_checks") or []) or "none"
    if mode == "heavy":
        return (
            "\n\nCognitive profile: HEAVY.\n"
            f"Reason: {(profile or {}).get('reason', 'risk escalation')}.\n"
            f"Required checks: {checks}.\n"
            "Do not answer live/current or memory-derived claims as true until current evidence supports them. "
            "If evidence is missing, say exactly what must be checked.\n"
        )
    return (
        "\n\nCognitive profile: DAILY.\n"
        "Answer with the normal low-latency path. You may use learned memories as orientation, but not as proof for live/current claims.\n"
    )


def _transcript_turn(m: dict) -> str:
    text = (m.get("text") or "").strip()
    if m.get("from") == "owner":
        return "Owner: " + text
    out = "Jarvis: " + text
    evidence = _compact_prior_thinking(m.get("evidence") or m.get("thinking") or "")
    if evidence:
        out += "\nJarvis prior evidence/status:\n" + evidence
    return out


def _compact_prior_thinking(thinking: str, limit: int = 2200) -> str:
    """Carry forward prior-turn operating evidence without dumping every thought token."""
    if not thinking:
        return ""
    status_lines = []
    marker_lines = []
    evidence_lines = []
    capture = False
    for raw in thinking.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("Harness:"):
            status_lines.append(line)
            capture = line.startswith("Harness: tool evidence")
            continue
        if line.startswith("JARVIS_EDIT "):
            marker_lines.append(line)
            continue
        if line.startswith("$ ") or line.startswith("/") or line.startswith("CODE:") or line.startswith("SOURCE:"):
            evidence_lines.append(line)
            continue
        if capture:
            evidence_lines.append(line[:500])
    markers = []
    seen = set()
    for line in marker_lines:
        if line not in seen:
            markers.append(line)
            seen.add(line)
    prefix = status_lines[-8:] + markers
    prefix_text = "\n".join(prefix)
    remaining = max(0, limit - len(prefix_text) - 2)
    tail_text = "\n".join(evidence_lines)[-remaining:] if remaining else ""
    return "\n".join(x for x in (prefix_text, tail_text) if x)[-limit:]


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token). Good enough for budgeting, not billing."""
    return max(0, len(text or "") // 4)


_CONVO_KINDS = ("message", "note", "answer", "question")


def maybe_compact_history(llm, messages: list[dict], window_tokens: int, keep_recent: int = 6,
                          status=None) -> tuple[list[dict], bool]:
    """Autocompaction: bound conversation growth against the model's REAL context window.

    If the transcript would exceed ~half the window, summarize the oldest turns into one synthetic
    note and keep the most recent turns verbatim — instead of silently dropping old context or
    overflowing the model. Returns (messages, compacted?). Short conversations pass through
    unchanged, so normal chats see no behavior change.
    """
    convo = [m for m in (messages or []) if m.get("kind") in _CONVO_KINDS]
    if not window_tokens or len(convo) <= keep_recent + 2:
        return messages, False
    transcript = "\n".join(_transcript_turn(m) for m in convo)
    if estimate_tokens(transcript) <= int(window_tokens * 0.5):
        return messages, False
    older, recent = convo[:-keep_recent], convo[-keep_recent:]
    older_text = "\n".join(_transcript_turn(m) for m in older)
    summary = ""
    try:
        summary = llm.run(
            "summarizer",
            "Compress this earlier portion of a chat between the owner and Jarvis into durable, "
            "factual notes — decisions made, facts established, open threads, and any file/command/"
            "endpoint references by name. Be concise; no preamble.\n\n" + older_text[-12000:],
            timeout=60,
        ).strip()
    except Exception:
        summary = ""
    if status:
        try:
            status(f"compacted {len(older)} earlier turns to fit the {window_tokens // 1000}K context window")
        except Exception:
            pass
    if not summary:
        return recent, True   # last resort: keep recent rather than overflow the model
    synth = {"id": "compacted-summary", "from": "jarvis", "kind": "note",
             "text": "[Earlier conversation compacted to fit the context window]\n" + summary}
    return [synth] + recent, True


def compact_evidence(text: str, limit: int = 4500) -> str:
    """Compress local docs/code/ops evidence into source lines safe to carry across turns."""
    if not text:
        return ""
    prefixes = (
        "SOURCE:", "TITLE:", "CODE:", "MATCHING_SYMBOLS:", "PROBE:",
        "MEMORY:", "GAP:", "STATUS:", "REASON:", "LEARNED:",
        "KIND:", "TRUST_POLICY:", "VOLATILITY:", "CONFIDENCE:", "STALE:", "VERIFIED:", "OBSERVED:",
    )
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(prefixes):
            lines.append(line[:700])
    return "\n".join(lines)[-limit:]


def compact_tool_evidence_for_storage(text: str, limit: int = 8000) -> str:
    """Store tool evidence without losing edit-card markers behind large file reads."""
    if not text or len(text) <= limit:
        return text or ""
    marker_lines = []
    seen_markers = set()
    for line in text.splitlines():
        if line.strip().startswith("JARVIS_EDIT ") and line not in seen_markers:
            marker_lines.append(line)
            seen_markers.add(line)
    marker_block = "\n".join(marker_lines)
    remaining = max(1000, limit - len(marker_block) - 80)
    tail = "\n".join(
        line for line in text[-remaining:].splitlines()
        if not line.strip().startswith("JARVIS_EDIT ")
    )
    parts = []
    if marker_block:
        parts.append(marker_block)
    parts.append("[tool evidence truncated; tail kept for follow-up context]")
    parts.append(tail)
    return "\n".join(parts)[-limit:]


def learn_owner_correction_now(owner_text: str) -> bool:
    """Persist explicit owner corrections before the next fresh conversation can ask about them."""
    text = (owner_text or "").strip()
    low = text.lower()
    if not any(s in low for s in (
        "correction:", "that was wrong", "that's wrong", "that is wrong",
        "you were wrong", "you got that wrong", "actually,"
    )):
        return False
    fact = re.sub(r"^\s*correction:\s*", "", text, flags=re.I).strip()
    if len(fact) < 8:
        return False
    try:
        return local_knowledge.record_learned_memory(
            fact,
            source="owner-correction",
            scope="project",
            keywords=sorted(list(local_knowledge._terms(fact)))[:12],
            evidence=text[:2000],
        )
    except Exception:
        return False


def likely_needs_tools(text: str) -> bool:
    low = (text or "").lower()
    hints = (
        "status", "doing", "stuck", "check", "inspect", "find", "search", "read", "look",
        "file", "folder", "repo", "docs", "documentation", "service", "timer", "log",
        "ticket", "pipeline", "worker", "deploy", "host", "network", "storage", "uptime",
        "fix", "change", "edit", "update", "write", "append", "create", "remember", "learn",
    )
    return any(h in low for h in hints)


def fast_local_answer(latest: str, local_docs: str) -> str:
    """Answer simple local-memory lookups without the full model loop."""
    text = local_docs or ""
    low = (latest or "").lower()
    explicit_recall = any(s in low for s in (
        "do you remember", "what did you learn", "what have you learned",
        "learned memory", "your memory", "known gap", "learning gap",
    ))
    direct_lookup = re.search(r"\b(what|who|where)\s+(?:is|are|was|were)\b", low) is not None
    knowledge_probe = any(s in low for s in ("do you know", "are you aware", "what do you know"))
    has_non_memory_evidence = any(s in text for s in (
        "SOURCE:", "Code index hits relevant to this question:", "Operational index hits relevant to this question:",
    ))
    asks_memory = explicit_recall or ((knowledge_probe or direct_lookup) and not has_non_memory_evidence)
    if asks_memory and "Learned memories relevant to this question:" in text:
        mem = _first_memory_hint(text)
        if mem:
            fact = mem.get("fact", "")
            trust = mem.get("trust_policy", "use_as_hint")
            stale = mem.get("stale", "no")
            volatility = mem.get("volatility", "stable")
            if trust == "can_use_directly" and stale != "yes" and volatility != "volatile":
                return fact
            if explicit_recall:
                return (
                    f"Memory hint: {fact}\n\n"
                    f"Trust policy: {trust}. Volatility: {volatility}. "
                    "I should verify it before treating it as current truth."
                )
    if asks_memory and "Learning gaps relevant to this question:" in text:
        gap = re.search(r"GAP:\s*(.+?)(?:\nSTATUS:|\Z)", text, re.S)
        reason = re.search(r"REASON:\s*(.+?)(?:\nRECORDED:|\Z)", text, re.S)
        q = (gap.group(1).strip() if gap else latest.strip())
        r = (reason.group(1).strip() if reason else "needs investigation")
        return f"Known unresolved gap: {q}\n\nWhat I still need: {r}"
    return ""


def _first_memory_hint(local_docs: str) -> dict | None:
    block = re.search(r"MEMORY:\s*(.+?)(?=\nMEMORY:|\nGAP:|\Z)", local_docs or "", re.S)
    if not block:
        return None
    lines = block.group(1).splitlines()
    fact = lines[0].strip() if lines else ""
    meta = {"fact": fact}
    for line in lines[1:]:
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip().lower()] = v.strip()
    return meta


def collect_tool_evidence(llm, base: str, latest: str, status=None, force: bool = False) -> str:
    """Let the selected model request Jarvis-owned tools until it has enough evidence."""
    if not force and not likely_needs_tools(latest):
        return ""
    evidence = []
    planned_calls = planned_tool_calls(latest)
    for call in planned_calls:
        if status:
            try:
                status(tool_status(call))
            except Exception:
                pass
        result = chat_tools.run_model_tool(call, latest)
        evidence.append(chat_tools.format_result(result))
    if any(call.get("tool") in ("write", "append", "edit") for call in planned_calls):
        return "\n\n".join(evidence)
    for _ in range(chat_tools.MAX_TOOL_STEPS):
        prompt = (
            base
            + "\n\n"
            + chat_tools.TOOL_PROTOCOL
            + ("\n\nTool evidence so far:\n" + "\n\n".join(evidence) if evidence else "")
            + "\n\nLatest owner message:\n"
            + latest
            + "\n\nIf a tool is needed, return exactly one JSON tool request. If enough evidence is available, return exactly NO_TOOL."
        )
        try:
            decision = llm.run("orchestrator", prompt, timeout=120).strip()
        except Exception:
            break
        call = chat_tools.parse_model_tool_call(decision)
        if not call:
            break
        if status:
            try:
                status(tool_status(call))
            except Exception:
                pass
        result = chat_tools.run_model_tool(call, latest)
        evidence.append(chat_tools.format_result(result))
        if call.get("tool") in ("write", "append", "edit"):
            break
    return "\n\n".join(evidence)


def planned_tool_calls(latest: str) -> list[dict]:
    """Deterministic first-pass probes for broad local status questions.

    Models are still free to ask for more tools after this, but common status questions should
    start from reality instead of model-invented checklists.
    """
    edit_plan = _planned_edit_calls(latest)
    if edit_plan:
        return edit_plan
    low = (latest or "").lower()
    if not any(w in low for w in ("status", "doing", "stuck", "running", "health", "pipeline", "worker", "service", "timer")):
        return []
    calls: list[dict] = [
        {"tool": "shell", "args": {"cmd": "pwd; hostname; hostname -I 2>/dev/null; date"}},
    ]
    if any(w in low for w in ("pipeline", "worker", "agent", "service", "timer", "deploy", "review", "finder", "fixer", "stuck")):
        calls.extend([
            {"tool": "shell", "args": {"cmd": "systemctl list-timers --all --no-pager | sed -n '1,180p'"}},
            {"tool": "shell", "args": {"cmd": "systemctl list-units --type=service --all --no-pager | sed -n '1,220p'"}},
            {"tool": "shell", "args": {"cmd": "ps -eo pid,etime,cmd --sort=etime | grep -Ei 'jarvis|agent|worker|pipeline|deploy|review|finder|fixer' | grep -v grep | tail -80 || true"}},
        ])
    return calls[:4]


def _planned_edit_calls(text: str) -> list[dict]:
    raw = (text or "").strip()
    if not re.search(r"\b(edit|replace|change|update)\b", raw, re.I):
        return []
    patterns = [
        r"\b(?:edit|change|update)\s+(?P<path>/\S+)\s+replacing\s+(?P<old>.+?)\s+with\s+(?P<new>.+?)(?:,\s*then\b|\.?$|$)",
        r"\breplace\s+(?P<old>.+?)\s+with\s+(?P<new>.+?)\s+in\s+(?P<path>/\S+)(?:,\s*then\b|\.?$|$)",
    ]
    for pat in patterns:
        m = re.search(pat, raw, re.I)
        if not m:
            continue
        path = _strip_token(m.group("path"))
        old = _strip_token(m.group("old"))
        new = _strip_token(m.group("new"))
        if path and old and new:
            return [
                {"tool": "read", "args": {"path": path}},
                {"tool": "edit", "args": {"path": path, "old": old, "new": new}},
                {"tool": "read", "args": {"path": path}},
            ]
    return []


def _strip_token(value: str) -> str:
    value = (value or "").strip().strip("`'\"")
    return re.sub(r"\s*(?:,?\s*then\s+.*)?$", "", value, flags=re.I).strip().strip("`'\"")


def tool_status(call: dict) -> str:
    """Human-visible progress text for the live worklog."""
    tool = (call or {}).get("tool") or "tool"
    args = (call or {}).get("args") or {}
    if tool == "shell":
        cmd = str(args.get("cmd") or "").strip()
        return f"checking shell: {cmd[:90]}" if cmd else "checking shell"
    if tool == "read":
        path = str(args.get("path") or "").strip()
        return f"reading {path[:100]}" if path else "reading file"
    if tool == "search":
        pat = str(args.get("pattern") or "").strip()
        path = str(args.get("path") or "").strip()
        return f"searching {path or 'workspace'} for {pat[:70]}" if pat else "searching files"
    if tool == "edit":
        path = str(args.get("path") or "").strip()
        return f"editing {path[:100]}" if path else "editing file"
    if tool in ("write", "append"):
        path = str(args.get("path") or "").strip()
        action = "appending to" if tool == "append" else "writing"
        return f"{action} {path[:100]}" if path else f"{action} file"
    return f"running {tool} tool"


def run_tool_calls(calls: list[dict], owner_text: str, status=None, limit: int = 8) -> str:
    """Execute already-parsed tool calls (e.g. NATIVE structured tool_calls from the model) and
    return formatted evidence. Shell stays safety-gated; write/edit stay owner-gated in run_model_tool."""
    evidence = []
    for call in (calls or [])[:limit]:
        if status:
            try:
                status(tool_status(call))
            except Exception:
                pass
        evidence.append(chat_tools.format_result(chat_tools.run_model_tool(call, owner_text)))
    return "\n\n".join(evidence)


def execute_recovered_tool_calls(text: str, owner_text: str, status=None, limit: int = 8) -> str:
    """Execute safe model-emitted tool calls recovered from text/thinking."""
    # Recover ALL three formats models actually use: fenced shell, XML/pseudo-XML tags, AND
    # JSON tool requests ({"tool":...,"args":...}). DeepSeek/OpenAI-style models emit JSON in
    # their content — omitting extract_json_tool_calls here silently drops a valid tool call and
    # the turn ends with "(no reply)" (the recurring "announces an action then does nothing" bug,
    # issue #3). Keep all three recoverers wired together.
    commands = chat_tools.extract_shell_commands(text, limit=limit)
    calls = chat_tools.extract_xml_tool_calls(text, limit=limit)
    calls += chat_tools.extract_json_tool_calls(text, limit=limit)
    if not commands and not calls:
        return ""
    evidence = []
    for cmd in commands:
        if status:
            try:
                status(f"executing recovered shell command: {cmd[:90]}")
            except Exception:
                pass
        evidence.append(chat_tools.format_result(chat_tools.run_shell(cmd, timeout=20)))
    for call in calls[: max(0, limit - len(commands))]:
        if status:
            try:
                status(tool_status(call))
            except Exception:
                pass
        evidence.append(chat_tools.format_result(chat_tools.run_model_tool(call, owner_text)))
    return "\n\n".join(evidence)


def repair_unexecuted_command_plan(llm, latest: str, answer_text: str, thinking_text: str = "",
                                   status=None) -> str:
    """If a model dumps tool commands instead of using tools, execute the safe plan and summarize."""
    combined = "\n\n".join(x for x in (answer_text, thinking_text) if x)
    ev = execute_recovered_tool_calls(combined, latest, status=status, limit=8)
    if not ev:
        return ""
    prompt = (
        "The model produced tool calls instead of a final answer. Jarvis executed the safe calls below.\n"
        "Give a concise, outcome-focused answer to the owner from this evidence. Do not include command blocks unless needed.\n\n"
        f"Owner message:\n{latest}\n\n"
        f"Executed evidence:\n{ev}\n"
    )
    try:
        out = llm.run("orchestrator", prompt, timeout=80).strip()
        return out or ev
    except Exception:
        return ev


def _json_object(text: str) -> dict | None:
    raw = (text or "").strip()
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def _verification_terms(owner_text: str, answer_text: str) -> list[str]:
    text = f"{owner_text}\n{answer_text}".lower()
    stop = {
        "with", "from", "that", "this", "there", "their", "checked", "answer",
        "evidence", "running", "service", "status", "returned", "today",
    }
    terms = []
    for raw in re.findall(r"[a-z0-9_./:@-]{4,}", text):
        term = raw.strip(".,;:()[]{}'\"`")
        if not term or term in stop or term in terms:
            continue
        if any(ch in term for ch in ".-/@:") or term in {"systemctl", "active", "inactive", "failed"}:
            terms.append(term)
        elif len(term) >= 7:
            terms.append(term)
    return terms[:40]


def _focused_evidence_block(block: str, terms: list[str], limit: int = 2600) -> str:
    block = (block or "").strip()
    if len(block) <= limit:
        return block
    lower = block.lower()
    header = block.splitlines()[0][:240]
    windows = [header]
    for term in terms:
        pos = lower.find(term)
        if pos < 0:
            continue
        start = max(0, pos - 800)
        end = min(len(block), pos + 1400)
        snippet = block[start:end].strip()
        if snippet and snippet not in windows:
            windows.append(snippet)
        if sum(len(x) for x in windows) >= limit:
            break
    return "\n...\n".join(windows)[:limit]


def _verification_evidence_pack(owner_text: str, answer_text: str, evidence: str,
                                limit: int = 16000) -> str:
    """Build verifier evidence around the claims instead of taking only the head."""
    evidence = evidence or ""
    if len(evidence) <= limit:
        return evidence
    terms = _verification_terms(owner_text, answer_text)
    blocks = [b.strip() for b in re.split(r"(?m)(?=^\$ )", evidence) if b.strip()]
    scored = []
    for idx, block in enumerate(blocks):
        low = block.lower()
        score = sum(1 for term in terms if term and term in low)
        if score:
            scored.append((score, idx, block))
    selected = sorted(scored, key=lambda row: (-row[0], row[1]))[:8]
    selected = sorted(selected, key=lambda row: row[1])
    parts = ["[focused evidence blocks matching the owner request and answer]"]
    for _, _, block in selected:
        focused = _focused_evidence_block(block, terms)
        if focused:
            parts.append(focused)
    parts.append("[evidence tail]")
    parts.append(evidence[-6000:])
    packed = "\n\n".join(parts)
    return packed[-limit:]


def verify_heavy_answer(llm, owner_text: str, answer_text: str, evidence: str, profile: dict) -> dict:
    """Heavy-mode self-check against gathered evidence. Fails closed when evidence is missing."""
    if (profile or {}).get("mode") != "heavy":
        return {"checked": False, "verified": True, "note": "daily profile"}
    if not (evidence or "").strip():
        return {"checked": True, "verified": False, "note": "no current evidence was gathered"}
    verifier_evidence = _verification_evidence_pack(owner_text, answer_text, evidence)
    prompt = (
        "You are Jarvis's heavy-mode verification pass. Check the answer only against the evidence. "
        "Do not judge style. Do not assume facts not in evidence. Reply as JSON only: "
        '{"verified":true|false,"note":"short reason"}.\n\n'
        f"Owner request:\n{owner_text}\n\n"
        f"Answer:\n{answer_text}\n\n"
        f"Evidence:\n{verifier_evidence}\n"
    )
    try:
        data = _json_object(llm.run("red_team", prompt, timeout=80).strip()) or {}
    except Exception as e:
        return {"checked": True, "verified": False, "note": f"verification failed: {str(e)[:120]}"}
    return {
        "checked": True,
        "verified": bool(data.get("verified")),
        "note": str(data.get("note") or "").strip()[:300],
    }


def reflect_and_learn(llm, cfg: dict, owner_text: str, answer_text: str,
                      local_docs: str = "", tool_evidence: str = "",
                      profile: dict | None = None, verified: bool = False) -> None:
    """Force a post-answer memory pass.

    The reflection may create a learned memory or learning debt. It must not memorize guesses.
    """
    try:
        local_knowledge.maybe_record_learning_gap(owner_text, answer_text, bool(local_docs or tool_evidence))
    except Exception:
        pass
    owner_low = (owner_text or "").lower()
    answer_low = (answer_text or "").lower()
    explicit_learn = any(s in owner_low for s in (
        "remember this", "learn this", "document this", "so next time", "next time know",
        "you should know", "add this to memory", "save this"
    ))
    correction = any(s in owner_low for s in (
        "wrong", "incorrect", "not true", "you missed", "that's not", "that is not",
        "you forgot", "you failed"
    ))
    weak_answer = any(s in answer_low for s in (
        "i don't know", "i do not know", "not aware", "no evidence", "missing",
        "don't have", "do not have"
    ))
    if explicit_learn or correction:
        try:
            local_knowledge.record_learned_memory(
                owner_text[:1600],
                source="owner",
                scope="project",
                keywords=sorted(list(local_knowledge._terms(owner_text)))[:12],
                evidence=(tool_evidence or local_docs or "Owner instruction/correction")[:2000],
                verified=verified,
            )
        except Exception:
            pass
    if weak_answer and not (local_docs or tool_evidence):
        try:
            local_knowledge.record_learning_gap(owner_text, answer_text, "missing-evidence-after-answer")
        except Exception:
            pass
    prompt = (
        "You are Jarvis's memory reflection step. Decide whether this turn produced a durable memory.\n"
        "Auto-learning is important, but memory is a hint, not truth. Only learn from owner corrections/instructions, local docs, or tool evidence. Do not memorize model guesses.\n"
        f"Execution profile: {(profile or {}).get('mode', 'daily')}. Verification passed: {bool(verified)}.\n"
        "Important: learn even when the owner did NOT say remember, if Jarvis discovered a durable fact it previously lacked.\n"
        "Durable facts include documentation roots, repo paths, service names, runbook locations, project rules, tool commands that are the canonical way to inspect something, and stable system architecture.\n"
        "Do NOT memorize volatile facts such as current queue counts, uptime, load averages, transient status, temporary failures, timestamps, or one-off command output unless the owner explicitly asks you to remember them.\n"
        "If Jarvis failed to answer because evidence/tools/context were missing, create learning debt.\n"
        "If there is nothing durable, reply exactly NO_MEMORY.\n"
        "If there is a durable learned fact, reply as JSON only:\n"
        '{"learn":true,"fact":"...","scope":"global|project|host|conversation","keywords":["..."],"source":"owner|tool|docs","kind":"preference|procedure|stable_fact|snapshot|volatile_status|warning","trust_policy":"use_as_hint|can_use_directly|must_verify_before_answer","volatility":"stable|snapshot|volatile","confidence":"low|medium|high"}\n'
        "If Jarvis lacked needed knowledge and must investigate later, reply as JSON only:\n"
        '{"gap":true,"question":"...","reason":"..."}\n\n'
        f"Owner message:\n{owner_text}\n\n"
        f"Jarvis answer:\n{answer_text}\n\n"
        f"Local docs evidence:\n{local_docs[:4000]}\n\n"
        f"Tool evidence:\n{tool_evidence[:4000]}\n"
    )
    try:
        decision = llm.run("orchestrator", prompt, timeout=80).strip()
    except Exception:
        return
    data = _json_object(decision)
    if not data:
        return
    try:
        if data.get("learn") and data.get("fact"):
            evidence = tool_evidence or local_docs or owner_text
            local_knowledge.record_learned_memory(
                str(data.get("fact") or ""),
                source=str(data.get("source") or "reflection"),
                scope=str(data.get("scope") or "project"),
                keywords=[str(k) for k in data.get("keywords", []) if str(k).strip()],
                evidence=evidence[:2000],
                kind=str(data.get("kind") or ""),
                trust_policy=str(data.get("trust_policy") or ""),
                volatility=str(data.get("volatility") or ""),
                confidence=str(data.get("confidence") or ""),
                verified=verified,
            )
        elif data.get("gap") and data.get("question"):
            local_knowledge.record_learning_gap(
                str(data.get("question") or owner_text),
                answer_text,
                str(data.get("reason") or "reflection-gap"),
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cognitive Loop — Task Record (durability) + run_heavy_task (non-streaming)
# ---------------------------------------------------------------------------

class TaskRecord:
    """Minimal persisted state machine for heavy-mode tasks.

    Checkpoints each stage so a process restart can resume (or safely finalize) instead of
    restarting from scratch. The state machine is intentionally minimal — no workflow engine,
    just a JSON snapshot per task. Heavy chat turns and tick tasks both use this.
    """

    STAGES = ("FRAME", "RECALL", "APPRAISE", "GROUND", "ACT", "VERIFY", "LEARN", "DONE")

    def __init__(self, task_id: str, owner_text: str, profile: dict) -> None:
        self.id = task_id
        self.owner_text = owner_text
        self.profile = profile
        self.stage = "FRAME"
        self.checkpoints: dict = {}
        self.created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._path = _TASK_DIR / f"{task_id}.json"

    @classmethod
    def create(cls, owner_text: str, profile: dict) -> "TaskRecord":
        cls._prune()                       # bound the checkpoint dir so it can't grow without limit
        task_id = _uuid.uuid4().hex[:10]
        t = cls(task_id, owner_text, profile)
        t._save()
        return t

    @classmethod
    def _prune(cls, keep: int = 40) -> None:
        """Keep only the newest `keep` checkpoint files. These are per-heavy-task JSON snapshots;
        nothing consumes the old ones (resume only looks at recent in-progress tasks), so without
        this the dir grows unbounded (was 94 stale files on the live box)."""
        try:
            files = sorted(_TASK_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for p in files[keep:]:
                p.unlink(missing_ok=True)
        except Exception:
            pass

    @classmethod
    def load_pending(cls) -> "TaskRecord | None":
        """Return the most recent in-progress heavy task (< 1 h old) for restart-resume."""
        try:
            _TASK_DIR.mkdir(parents=True, exist_ok=True)
            files = sorted(_TASK_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for path in files[:10]:
                try:
                    data = json.loads(path.read_text())
                    if data.get("stage") in ("DONE", None):
                        continue
                    age = time.time() - float(data.get("mtime") or 0)
                    if age > 3600:
                        continue
                    t = cls(data["id"], data["owner_text"], data.get("profile") or {})
                    t.stage = data["stage"]
                    t.checkpoints = data.get("checkpoints") or {}
                    t.created_at = data.get("created_at", "")
                    t._path = path
                    return t
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def advance(self, stage: str, checkpoint_data: dict | None = None) -> None:
        """Move to the next stage and persist a checkpoint."""
        self.stage = stage
        if checkpoint_data:
            self.checkpoints[stage] = checkpoint_data
        self._save()

    def finalize(self, outcome: str) -> None:
        self.stage = "DONE"
        self.checkpoints["DONE"] = {"outcome": outcome, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._save()

    def _save(self) -> None:
        _TASK_DIR.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({
            "id": self.id,
            "owner_text": self.owner_text[:2000],
            "profile": self.profile,
            "stage": self.stage,
            "checkpoints": self.checkpoints,
            "created_at": self.created_at,
            "mtime": time.time(),
        }, indent=2))


def run_heavy_task(llm, cfg: dict, owner_text: str, profile: dict,
                   status=None, task: "TaskRecord | None" = None) -> dict:
    """Non-streaming heavy cognitive loop: RECALL → APPRAISE → GROUND → ACT → VERIFY → LEARN.

    Used by the autonomous tick (no SSE push needed). Uses Brain.turn() — the provider-agnostic
    contract — so HTTP backends get native structured tool_calls and CLI backends get text
    with recovery. Returns {text, evidence, verified, verification_note}.
    """
    task = task or TaskRecord.create(owner_text, profile)

    def _st(msg: str) -> None:
        if status:
            try:
                status(msg)
            except Exception:
                pass

    # RECALL
    task.advance("RECALL")
    _st("heavy task: RECALL — loading context and local docs")
    hctx = build_context(cfg, [], owner_text)
    base = hctx["base"]
    local_docs = hctx.get("local_docs", "")
    task.advance("APPRAISE", {"local_docs_length": len(local_docs)})

    # GROUND — data-first evidence from reality (structural cure for stale-replay)
    _st("heavy task: GROUND — gathering live evidence")
    tool_evidence = collect_tool_evidence(
        llm, base + profile_prompt(profile), owner_text, status=_st, force=True
    )
    task.advance("GROUND", {"evidence_length": len(tool_evidence)})

    # SUFFICIENCY + ACT via Brain.turn() (provider-agnostic call)
    _st("heavy task: ACT — generating answer from evidence")
    act_prompt = (
        base + profile_prompt(profile)
        + (f"\n\nLOCAL TOOL EVIDENCE:\n{tool_evidence}\n" if tool_evidence else "")
        + "\nAnswer the task based on the evidence above. Be concise and outcome-focused."
        + "\n\nTask:\n" + owner_text
    )
    turn_result = llm.turn("orchestrator", act_prompt, tools=chat_tools.TOOL_SPECS, timeout=180)
    answer_text = turn_result.get("text", "")

    # Execute any native tool calls the model used, then re-generate with the extra evidence
    native_calls = []
    for tj in turn_result.get("tool_calls", []):
        try:
            obj = json.loads(tj)
            a = obj.get("arguments")
            a = json.loads(a) if isinstance(a, str) else (a or {})
            call = chat_tools.normalize_tool_call(obj.get("name"), a if isinstance(a, dict) else {})
            if call:
                native_calls.append(call)
        except Exception:
            pass
    if native_calls:
        extra_ev = run_tool_calls(native_calls, owner_text, status=_st)
        if extra_ev:
            tool_evidence = "\n\n".join(x for x in (tool_evidence, extra_ev) if x)
            regen = llm.turn("orchestrator",
                             act_prompt.replace(tool_evidence.split("\n\n")[0] if tool_evidence else "",
                                                tool_evidence),
                             timeout=120)
            answer_text = regen.get("text", "") or answer_text
    # CLI backends: scan text for tool calls and re-run (text-recovery path)
    if not native_calls:
        combined = (turn_result.get("text", "") + "\n" + turn_result.get("thinking", "")).strip()
        recovered = execute_recovered_tool_calls(combined, owner_text, status=_st, limit=3)
        if recovered:
            tool_evidence = "\n\n".join(x for x in (tool_evidence, recovered) if x)
            regen_prompt = (
                base + profile_prompt(profile)
                + f"\n\nLOCAL TOOL EVIDENCE:\n{tool_evidence}\n"
                + "\nAnswer the task based on the evidence above. Be concise and outcome-focused."
                + "\n\nTask:\n" + owner_text
            )
            regen = llm.turn("orchestrator", regen_prompt, timeout=120)
            answer_text = regen.get("text", "") or answer_text

    task.advance("ACT", {"answer_length": len(answer_text)})

    # VERIFY — self-check answer against reality (same model, fresh framing)
    _st("heavy task: VERIFY — checking answer against evidence")
    verification = verify_heavy_answer(llm, owner_text, answer_text, tool_evidence, profile)
    task.advance("VERIFY", {
        "verified": verification.get("verified"),
        "note": verification.get("note", ""),
    })

    # LEARN — always runs after heavy path; conservative trust policy by default
    _st("heavy task: LEARN — recording durable findings")
    try:
        reflect_and_learn(
            llm, cfg, owner_text, answer_text,
            local_docs=local_docs, tool_evidence=tool_evidence,
            profile=profile, verified=bool(verification.get("verified")),
        )
    except Exception:
        pass

    outcome = "verified" if verification.get("verified") else "unverified"
    task.finalize(f"{outcome}: {answer_text[:120]}")

    return {
        "text": answer_text,
        "evidence": tool_evidence,
        "verified": verification.get("verified", False),
        "verification_note": verification.get("note", ""),
    }
