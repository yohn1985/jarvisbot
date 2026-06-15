"""Provider-neutral Jarvis chat harness.

The model is only the reasoning brain. Jarvis owns context loading, tool execution,
evidence accumulation, and the reflection pass that creates durable learning.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from jarvis import chat_tools, local_knowledge, persona


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
            "\n\nThe owner gave you documentation path(s) that were durably remembered: "
            + ", ".join(remembered_roots)
            + "\n"
            if remembered_roots else ""
        )
        + "\n\nTool and evidence contract:\n"
        + "- Use available tools when local evidence is needed.\n"
        + f"- Current tool mode: {chat_tools.mode_description()}.\n"
        + "- Do not claim you ran, scanned, read, indexed, remembered, deployed, or checked anything unless evidence in this prompt proves it.\n"
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


def _transcript_turn(m: dict) -> str:
    text = (m.get("text") or "").strip()
    if m.get("from") == "owner":
        return "Owner: " + text
    out = "Jarvis: " + text
    evidence = _compact_prior_thinking(m.get("thinking") or "")
    if evidence:
        out += "\nJarvis prior evidence/status:\n" + evidence
    return out


def _compact_prior_thinking(thinking: str, limit: int = 2200) -> str:
    """Carry forward prior-turn operating evidence without dumping every thought token."""
    if not thinking:
        return ""
    lines = []
    capture = False
    for raw in thinking.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("Harness:"):
            lines.append(line)
            capture = line.startswith("Harness: tool evidence")
            continue
        if line.startswith("$ ") or line.startswith("/") or line.startswith("CODE:") or line.startswith("SOURCE:"):
            lines.append(line)
            continue
        if capture:
            lines.append(line[:500])
    return "\n".join(lines)[-limit:]


def compact_evidence(text: str, limit: int = 4500) -> str:
    """Compress local docs/code/ops evidence into source lines safe to carry across turns."""
    if not text:
        return ""
    prefixes = (
        "SOURCE:", "TITLE:", "CODE:", "MATCHING_SYMBOLS:", "PROBE:",
        "MEMORY:", "GAP:", "STATUS:", "REASON:", "LEARNED:",
    )
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(prefixes):
            lines.append(line[:700])
    return "\n".join(lines)[-limit:]


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
    knowledge_probe = any(s in low for s in ("do you know", "are you aware", "what do you know"))
    has_non_memory_evidence = any(s in text for s in (
        "SOURCE:", "Code index hits relevant to this question:", "Operational index hits relevant to this question:",
    ))
    asks_memory = explicit_recall or (knowledge_probe and not has_non_memory_evidence)
    if asks_memory and "Learned memories relevant to this question:" in text:
        mem = re.search(r"MEMORY:\s*(.+?)(?:\nSCOPE:|\Z)", text, re.S)
        if mem:
            return mem.group(1).strip()
    if asks_memory and "Learning gaps relevant to this question:" in text:
        gap = re.search(r"GAP:\s*(.+?)(?:\nSTATUS:|\Z)", text, re.S)
        reason = re.search(r"REASON:\s*(.+?)(?:\nRECORDED:|\Z)", text, re.S)
        q = (gap.group(1).strip() if gap else latest.strip())
        r = (reason.group(1).strip() if reason else "needs investigation")
        return f"Known unresolved gap: {q}\n\nWhat I still need: {r}"
    return ""


def collect_tool_evidence(llm, base: str, latest: str, status=None) -> str:
    """Let the selected model request Jarvis-owned tools until it has enough evidence."""
    if not likely_needs_tools(latest):
        return ""
    evidence = []
    for call in planned_tool_calls(latest):
        if status:
            try:
                status(tool_status(call))
            except Exception:
                pass
        result = chat_tools.run_model_tool(call, latest)
        evidence.append(chat_tools.format_result(result))
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


def repair_unexecuted_command_plan(llm, latest: str, answer_text: str, thinking_text: str = "",
                                   status=None) -> str:
    """If a model dumps tool commands instead of using tools, execute the safe plan and summarize."""
    combined = "\n\n".join(x for x in (answer_text, thinking_text) if x)
    commands = chat_tools.extract_shell_commands(combined, limit=8)
    calls = chat_tools.extract_xml_tool_calls(combined, limit=8)
    if not commands and not calls:
        return ""
    evidence = []
    for cmd in commands:
        if status:
            try:
                status(f"executing recovered shell command: {cmd[:90]}")
            except Exception:
                pass
        result = chat_tools.run_shell(cmd, timeout=20)
        evidence.append(chat_tools.format_result(result))
    for call in calls:
        if status:
            try:
                status(f"executing recovered {call.get('tool')} tool...")
            except Exception:
                pass
        result = chat_tools.run_model_tool(call, latest)
        evidence.append(chat_tools.format_result(result))
    ev = "\n\n".join(evidence)
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


def reflect_and_learn(llm, cfg: dict, owner_text: str, answer_text: str,
                      local_docs: str = "", tool_evidence: str = "") -> None:
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
        "Only learn from owner corrections/instructions, local docs, or tool evidence. Do not memorize model guesses.\n"
        "Important: learn even when the owner did NOT say remember, if Jarvis discovered a durable fact it previously lacked.\n"
        "Durable facts include documentation roots, repo paths, service names, runbook locations, project rules, tool commands that are the canonical way to inspect something, and stable system architecture.\n"
        "Do NOT memorize volatile facts such as current queue counts, uptime, load averages, transient status, temporary failures, timestamps, or one-off command output unless the owner explicitly asks you to remember them.\n"
        "If Jarvis failed to answer because evidence/tools/context were missing, create learning debt.\n"
        "If there is nothing durable, reply exactly NO_MEMORY.\n"
        "If there is a durable learned fact, reply as JSON only:\n"
        '{"learn":true,"fact":"...","scope":"global|project|host|conversation","keywords":["..."],"source":"owner|tool|docs"}\n'
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
            )
        elif data.get("gap") and data.get("question"):
            local_knowledge.record_learning_gap(
                str(data.get("question") or owner_text),
                answer_text,
                str(data.get("reason") or "reflection-gap"),
            )
    except Exception:
        pass
