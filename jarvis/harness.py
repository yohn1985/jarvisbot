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
        ("Owner: " if m.get("from") == "owner" else "Jarvis: ") + (m.get("text") or "")
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
    asks_memory = any(s in low for s in (
        "what do you know", "do you know", "are you aware", "what is", "what's",
        "where is", "where's", "where are", "where do"
    ))
    if asks_memory and "Learning gaps relevant to this question:" in text:
        gap = re.search(r"GAP:\s*(.+?)(?:\nSTATUS:|\Z)", text, re.S)
        reason = re.search(r"REASON:\s*(.+?)(?:\nRECORDED:|\Z)", text, re.S)
        q = (gap.group(1).strip() if gap else latest.strip())
        r = (reason.group(1).strip() if reason else "needs investigation")
        return f"Known unresolved gap: {q}\n\nWhat I still need: {r}"
    if asks_memory and "Learned memories relevant to this question:" in text:
        mem = re.search(r"MEMORY:\s*(.+?)(?:\nSCOPE:|\Z)", text, re.S)
        if mem:
            return mem.group(1).strip()
    return ""


def collect_tool_evidence(llm, base: str, latest: str, status=None) -> str:
    """Let the selected model request Jarvis-owned tools until it has enough evidence."""
    if not likely_needs_tools(latest):
        return ""
    evidence = []
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
                status(f"running {call.get('tool')} tool...")
            except Exception:
                pass
        result = chat_tools.run_model_tool(call, latest)
        evidence.append(chat_tools.format_result(result))
        if not result.get("ok") and call.get("tool") in ("write", "append"):
            break
    return "\n\n".join(evidence)


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
