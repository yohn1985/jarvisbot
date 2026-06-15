"""Local documentation memory for chat.

This is deliberately small and boring: discover likely documentation roots, keep
bounded metadata on disk, and retrieve source snippets before the model answers.
It is not a vector database and it does not pretend to read files it did not
actually inspect.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
KNOW_DIR = ROOT / "workspace" / "knowledge"
DOC_ROOTS = KNOW_DIR / "doc-roots.json"
DOC_INDEX = KNOW_DIR / "local-docs-index.json"
LEARNING_DEBT = KNOW_DIR / "learning-debt.jsonl"
LEARNING_GAPS_DIR = KNOW_DIR / "learning-gaps"
LEARNED = KNOW_DIR / "learned.jsonl"
LEARNED_DIR = KNOW_DIR / "learned"
QUESTIONS = KNOW_DIR / "questions.json"

MAX_ROOTS = 40
MAX_FILES = 700
MAX_FILE_BYTES = 220_000
MAX_TEXT = 16_000
STALE_SECONDS = 15 * 60
DOC_EXTS = {".md", ".txt", ".rst", ".json", ".yaml", ".yml"}
SPECIAL_FILES = {"AGENTS.md", "PROJECT.md", "WORK_IN_PROGRESS.md", "CONTEXT.md", "README.md"}
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".cache",
    ".next", "dist", "build", "target", ".tmp", "tmp", "logs",
}
STOPWORDS = {
    "the", "a", "an", "is", "are", "of", "to", "in", "on", "for", "what", "which",
    "how", "does", "do", "this", "that", "and", "or", "its", "it", "be", "with",
    "by", "any", "about", "know", "you", "your", "me", "my", "can", "tell",
}


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _config_roots(cfg: dict | None) -> list[Path]:
    cfg = cfg or {}
    roots: list[str] = []
    for section, key in (("knowledge", "doc_roots"), ("documentation", "roots")):
        value = (cfg.get(section, {}) or {}).get(key, [])
        if isinstance(value, str):
            roots.append(value)
        elif isinstance(value, list):
            roots.extend(str(v) for v in value)
    return [Path(os.path.expandvars(os.path.expanduser(r))) for r in roots if str(r).strip()]


def remembered_roots() -> list[Path]:
    data = _read_json(DOC_ROOTS, {"roots": []})
    return [Path(x) for x in data.get("roots", []) if isinstance(x, str)]


def remember_root(path: str | Path, source: str = "owner") -> bool:
    p = Path(str(path)).expanduser()
    try:
        p = p.resolve()
    except Exception:
        pass
    if not p.exists():
        return False
    data = _read_json(DOC_ROOTS, {"roots": [], "sources": {}})
    roots = list(dict.fromkeys([*data.get("roots", []), str(p)]))[:MAX_ROOTS]
    sources = dict(data.get("sources", {}))
    sources[str(p)] = {"source": source, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _write_json(DOC_ROOTS, {"roots": roots, "sources": sources})
    return True


def remember_roots_from_text(text: str, cfg: dict | None = None) -> list[str]:
    """Persist explicit absolute paths the owner tells Jarvis to remember."""
    found: list[str] = []
    for raw in re.findall(r"(/[A-Za-z0-9._~+@%=-][A-Za-z0-9._~+@%=/:-]*)(?=\s|$)", text or ""):
        cleaned = raw.strip().rstrip(".,;:")
        p = Path(cleaned)
        name = p.name.lower()
        if name in {"docs", "documentation"} or p.is_file() and p.name in SPECIAL_FILES:
            if remember_root(p, source="owner-chat"):
                found.append(str(p.resolve()))
    if found:
        refresh_index(cfg, force=True)
    return found


def _default_roots() -> list[Path]:
    roots = [
        ROOT / "docs",
        ROOT / "workspace" / "discovery",
        ROOT / "workspace" / "knowledge",
    ]
    for env in ("JARVIS_WORKSPACE", "JARVIS_CONTEXT_ROOT"):
        raw = os.environ.get(env)
        if raw:
            roots.append(Path(raw).expanduser())
    return roots


def _discover_doc_roots(base: Path, max_depth: int = 3) -> list[Path]:
    roots: list[Path] = []
    stack: list[tuple[Path, int]] = [(base, 0)]
    seen = 0
    while stack and seen < 900:
        cur, depth = stack.pop()
        seen += 1
        try:
            entries = list(os.scandir(cur))
        except Exception:
            continue
        for entry in entries:
            if entry.name in SKIP_DIRS:
                continue
            try:
                if entry.is_file() and entry.name in SPECIAL_FILES:
                    roots.append(Path(entry.path).parent)
                elif entry.is_dir():
                    if entry.name.lower() in {"docs", "documentation"}:
                        roots.append(Path(entry.path))
                    if depth < max_depth:
                        stack.append((Path(entry.path), depth + 1))
            except OSError:
                continue
        if len(roots) >= MAX_ROOTS:
            break
    return roots


def roots(cfg: dict | None = None) -> list[Path]:
    out: list[Path] = []
    for p in [*_config_roots(cfg), *remembered_roots(), *_default_roots()]:
        try:
            rp = p.resolve()
        except Exception:
            rp = p
        if rp.exists() and rp not in out:
            out.append(rp)
        if len(out) >= MAX_ROOTS:
            break
    return out


def _iter_doc_files(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    stack = [root]
    seen_dirs = 0
    while stack and seen_dirs < 500:
        cur = stack.pop()
        seen_dirs += 1
        try:
            entries = list(os.scandir(cur))
        except Exception:
            continue
        for entry in entries:
            if entry.name in SKIP_DIRS:
                continue
            p = Path(entry.path)
            try:
                if entry.is_dir():
                    stack.append(p)
                elif entry.is_file() and (p.suffix.lower() in DOC_EXTS or p.name in SPECIAL_FILES):
                    yield p
            except OSError:
                continue


def _read_doc(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[:MAX_TEXT]
    except Exception:
        return ""


def _summarize(path: Path, text: str) -> dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    title = next((l.lstrip("# ").strip() for l in lines if l.startswith("#")), path.name)
    snippet = " ".join(lines[:12])[:1200]
    st = path.stat()
    return {
        "path": str(path),
        "name": path.name,
        "title": title[:160],
        "snippet": snippet,
        "mtime": int(st.st_mtime),
        "size": int(st.st_size),
    }


def refresh_index(cfg: dict | None = None, force: bool = False) -> dict:
    existing = _read_json(DOC_INDEX, {})
    if not force and existing.get("indexed_at") and time.time() - float(existing["indexed_at"]) < STALE_SECONDS:
        return existing
    items: list[dict] = []
    seen: set[str] = set()
    for root in roots(cfg):
        for path in _iter_doc_files(root):
            if str(path) in seen:
                continue
            seen.add(str(path))
            text = _read_doc(path)
            if not text:
                continue
            try:
                items.append(_summarize(path, text))
            except Exception:
                continue
            if len(items) >= MAX_FILES:
                break
        if len(items) >= MAX_FILES:
            break
    data = {
        "indexed_at": time.time(),
        "roots": [str(p) for p in roots(cfg)],
        "files": items,
    }
    _write_json(DOC_INDEX, data)
    return data


def _terms(query: str) -> set[str]:
    raw = re.findall(r"[a-z0-9_./:-]+", (query or "").lower())
    terms = {t for t in raw if len(t) > 2 and t not in STOPWORDS}
    for token in list(terms):
        for part in re.split(r"[-_/.:]+", token):
            if len(part) > 2 and part not in STOPWORDS:
                terms.add(part)
    return terms


def retrieve(query: str, cfg: dict | None = None, limit: int = 6) -> str:
    terms = _terms(query)
    if not terms:
        return ""
    data = refresh_index(cfg)
    scored: list[tuple[int, dict]] = []
    for item in data.get("files", []):
        hay = " ".join(str(item.get(k, "")) for k in ("path", "name", "title", "snippet")).lower()
        score = 0
        for term in terms:
            if term in hay:
                score += 4 if term in str(item.get("title", "")).lower() else 1
        if score:
            scored.append((score, item))
    scored.sort(key=lambda pair: (-pair[0], pair[1].get("path", "")))
    if not scored:
        gap_lines = _retrieve_learning_gaps(query)
        return gap_lines
    lines = [
        "Use only these local documentation snippets as evidence for this machine when relevant.",
        "Do not claim to have scanned, indexed, read, or remembered any other local file.",
    ]
    for score, item in scored[:limit]:
        lines.append(f"\nSOURCE: {item.get('path')}\nTITLE: {item.get('title')}\nSNIPPET: {item.get('snippet')}")
    gap_lines = _retrieve_learning_gaps(query)
    if gap_lines:
        lines.append("\n" + gap_lines)
    return "\n".join(lines)[:9000]


def _retrieve_learning_gaps(query: str, limit: int = 4) -> str:
    terms = _terms(query)
    if not terms:
        return ""
    hits = []
    try:
        if LEARNING_DEBT.exists():
            for line in LEARNING_DEBT.read_text(encoding="utf-8").splitlines()[-300:]:
                if not line.strip():
                    continue
                row = json.loads(line)
                hay = " ".join(str(row.get(k, "")) for k in ("question", "answer", "reason", "status")).lower()
                score = sum(1 for t in terms if t in hay)
                if score:
                    hits.append((score, row))
    except Exception:
        pass
    if not hits:
        return ""
    hits.sort(key=lambda pair: -pair[0])
    lines = [
        "Learning gaps relevant to this question:",
        "These are unresolved known-unknowns. Do not present them as facts; use them to explain what still needs investigation.",
    ]
    seen = set()
    for _, row in hits[:limit]:
        q = str(row.get("question") or "").strip()
        if not q or q.lower() in seen:
            continue
        seen.add(q.lower())
        lines.append(
            f"\nGAP: {q}\nSTATUS: {row.get('status', 'needs-investigation')}\nREASON: {row.get('reason', '')}\nRECORDED: {row.get('ts', '')}"
        )
    return "\n".join(lines)[:5000] if len(lines) > 2 else ""


def record_learning_gap(question: str, answer: str = "", reason: str = "unknown") -> None:
    KNOW_DIR.mkdir(parents=True, exist_ok=True)
    LEARNING_GAPS_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "question": (question or "")[:1000],
        "answer": (answer or "")[:1000],
        "reason": reason,
        "status": "needs-investigation",
    }
    with LEARNING_DEBT.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    slug = "-".join(sorted(_terms(row["question"]))[:10]) or f"gap-{int(time.time())}"
    path = LEARNING_GAPS_DIR / f"{int(time.time())}-{slug[:80]}.md"
    path.write_text(
        "\n".join([
            "# Learning gap",
            "",
            f"- Status: {row['status']}",
            f"- Reason: {row['reason']}",
            f"- Recorded: {row['ts']}",
            "",
            "## Question",
            "",
            row["question"],
            "",
            "## Last answer",
            "",
            row["answer"],
        ]).strip() + "\n",
        encoding="utf-8",
    )
    try:
        refresh_index(force=True)
    except Exception:
        pass
    queue_learning_question(question, reason)


def record_learned_memory(fact: str, source: str = "reflection", scope: str = "project",
                          keywords: list[str] | None = None, evidence: str = "") -> bool:
    """Persist a verified learned fact as both JSONL and an indexed markdown note."""
    fact = (fact or "").strip()
    if len(fact) < 8:
        return False
    wanted = _terms(fact)
    try:
        if LEARNED.exists():
            for line in LEARNED.read_text(encoding="utf-8").splitlines()[-200:]:
                if not line.strip():
                    continue
                row = json.loads(line)
                existing = _terms(str(row.get("fact") or ""))
                if fact.lower() == str(row.get("fact") or "").lower():
                    return False
                if wanted and existing and len(wanted & existing) >= max(3, min(len(wanted), len(existing)) // 2):
                    return False
    except Exception:
        pass
    KNOW_DIR.mkdir(parents=True, exist_ok=True)
    LEARNED_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "fact": fact[:2000],
        "source": source[:80],
        "scope": (scope or "project")[:40],
        "keywords": [str(k)[:80] for k in (keywords or [])[:20]],
        "evidence": (evidence or "")[:2000],
    }
    with LEARNED.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    slug_terms = _terms(" ".join([fact, " ".join(row["keywords"])]))
    slug = "-".join(sorted(slug_terms)[:8]) or f"learned-{int(time.time())}"
    path = LEARNED_DIR / f"{int(time.time())}-{slug[:80]}.md"
    body = [
        "# Learned memory",
        "",
        f"- Scope: {row['scope']}",
        f"- Source: {row['source']}",
        f"- Learned at: {row['ts']}",
        f"- Keywords: {', '.join(row['keywords'])}",
        "",
        "## Fact",
        "",
        row["fact"],
    ]
    if row["evidence"]:
        body += ["", "## Evidence", "", row["evidence"]]
    path.write_text("\n".join(body).strip() + "\n", encoding="utf-8")
    refresh_index(force=True)
    return True


def queue_learning_question(question: str, reason: str = "missing-local-knowledge") -> bool:
    """Feed owner-discovered gaps into the existing curiosity queue."""
    q = (question or "").strip()
    if len(q) < 8:
        return False
    queue = _read_json(QUESTIONS, [])
    if not isinstance(queue, list):
        queue = []
    wanted = _terms(q)
    for item in queue:
        if not isinstance(item, dict):
            continue
        existing = _terms(str(item.get("q") or ""))
        if wanted and existing and len(wanted & existing) >= max(2, min(len(wanted), len(existing)) // 2):
            return False
    queue.append({
        "id": f"owner-{int(time.time())}",
        "q": q,
        "answered": False,
        "answer": None,
        "source": "owner-chat",
        "reason": reason,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    _write_json(QUESTIONS, queue)
    return True


def maybe_record_learning_gap(owner_text: str, reply_text: str, had_context: bool) -> None:
    owner = (owner_text or "").lower()
    reply = (reply_text or "").lower()
    asks_knowledge = any(s in owner for s in ("do you know", "are you aware", "remember", "document this", "learn this"))
    correction = any(s in owner for s in ("wrong", "incorrect", "not true", "you missed", "next time", "so that next time"))
    weak_reply = any(s in reply for s in ("i don't know", "i do not know", "don't have", "do not have", "not aware", "no explicit knowledge"))
    if correction or (asks_knowledge and (weak_reply or not had_context)):
        record_learning_gap(owner_text, reply_text, "owner-correction" if correction else "missing-local-knowledge")
