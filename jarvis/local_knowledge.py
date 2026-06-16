"""Local documentation memory for chat.

This is deliberately small and boring: discover likely documentation roots, keep
bounded metadata on disk, and retrieve source snippets before the model answers.
It is not a vector database and it does not pretend to read files it did not
actually inspect.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
KNOW_DIR = ROOT / "workspace" / "knowledge"
DOC_ROOTS = KNOW_DIR / "doc-roots.json"
DOC_INDEX = KNOW_DIR / "local-docs-index.json"
CODE_INDEX = KNOW_DIR / "local-code-index.json"
OPS_INDEX = KNOW_DIR / "local-ops-index.json"
LEARNING_DEBT = KNOW_DIR / "learning-debt.jsonl"
LEARNING_GAPS_DIR = KNOW_DIR / "learning-gaps"
LEARNED = KNOW_DIR / "learned.jsonl"
LEARNED_DIR = KNOW_DIR / "learned"
QUESTIONS = KNOW_DIR / "questions.json"
DEFAULT_HINT_TTL_SECONDS = 30 * 24 * 3600
VOLATILE_TTL_SECONDS = 3600

MAX_ROOTS = 40
MAX_FILES = 700
MAX_CODE_FILES = 900
MAX_FILE_BYTES = 220_000
MAX_TEXT = 16_000
STALE_SECONDS = 15 * 60
DOC_EXTS = {".md", ".txt", ".rst", ".json", ".yaml", ".yml"}
CODE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java",
    ".kt", ".swift", ".php", ".rb", ".cs", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".sh", ".bash", ".zsh", ".sql", ".html", ".css", ".scss",
}
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


def workspace_roots(cfg: dict | None = None) -> list[Path]:
    """Generic roots for code/ops indexing. Config/env/remembered roots win; no owner-specific paths."""
    cfg = cfg or {}
    candidates: list[Path] = []
    for section, key in (("workspace", "root"), ("context", "root")):
        raw = (cfg.get(section, {}) or {}).get(key)
        if raw:
            candidates.append(Path(os.path.expandvars(os.path.expanduser(str(raw)))))
    for section, key in (("context", "roots"), ("workspace", "roots")):
        value = (cfg.get(section, {}) or {}).get(key, [])
        if isinstance(value, str):
            candidates.append(Path(os.path.expandvars(os.path.expanduser(value))))
        elif isinstance(value, list):
            candidates.extend(Path(os.path.expandvars(os.path.expanduser(str(v)))) for v in value)
    for env in ("JARVIS_WORKSPACE", "JARVIS_CONTEXT_ROOT"):
        raw = os.environ.get(env)
        if raw:
            candidates.append(Path(raw).expanduser())
    candidates.extend(remembered_roots())
    candidates.append(ROOT)

    out: list[Path] = []
    for p in candidates:
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


def _iter_code_files(root: Path) -> Iterable[Path]:
    if root.is_file():
        if root.suffix.lower() in CODE_EXTS:
            yield root
        return
    stack = [(root, 0)]
    seen_dirs = 0
    while stack and seen_dirs < 900:
        cur, depth = stack.pop()
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
                    if depth < 5:
                        stack.append((p, depth + 1))
                elif entry.is_file() and p.suffix.lower() in CODE_EXTS:
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
    old_by_path = {str(item.get("path")): item for item in existing.get("files", []) if isinstance(item, dict)}
    for root in roots(cfg):
        for path in _iter_doc_files(root):
            if str(path) in seen:
                continue
            seen.add(str(path))
            try:
                st = path.stat()
                old = old_by_path.get(str(path))
                if old and int(old.get("mtime", -1)) == int(st.st_mtime) and int(old.get("size", -1)) == int(st.st_size):
                    items.append(old)
                    if len(items) >= MAX_FILES:
                        break
                    continue
            except Exception:
                pass
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
    learned_lines = _retrieve_learned_memories(query)
    gap_lines = _retrieve_learning_gaps(query)
    code_lines = _retrieve_code_index(query, cfg)
    ops_lines = _retrieve_ops_index(query, cfg)
    if not scored:
        return "\n\n".join(x for x in (learned_lines, code_lines, ops_lines, gap_lines) if x)
    lines = [
        "Use only these local documentation snippets as evidence for this machine when relevant.",
        "Do not claim to have scanned, indexed, read, or remembered any other local file.",
    ]
    if learned_lines:
        lines.append(learned_lines)
    if code_lines:
        lines.append(code_lines)
    if ops_lines:
        lines.append(ops_lines)
    for score, item in scored[:limit]:
        lines.append(f"\nSOURCE: {item.get('path')}\nTITLE: {item.get('title')}\nSNIPPET: {item.get('snippet')}")
    if gap_lines:
        lines.append("\n" + gap_lines)
    return "\n".join(lines)[:9000]


def _symbol_rows(path: Path, text: str) -> list[dict]:
    out: list[dict] = []
    patterns = [
        re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][\w]*)\s*\("),
        re.compile(r"^\s*class\s+([A-Za-z_][\w]*)\b"),
        re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("),
        re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(?[^=]*=>"),
        re.compile(r"^\s*(?:func|fn)\s+([A-Za-z_][\w]*)\s*\("),
    ]
    for lineno, line in enumerate(text.splitlines(), 1):
        if len(out) >= 80:
            break
        for pat in patterns:
            m = pat.search(line)
            if m:
                out.append({"name": m.group(1), "line": lineno, "kind": "symbol"})
                break
    return out


def refresh_code_index(cfg: dict | None = None, force: bool = False) -> dict:
    existing = _read_json(CODE_INDEX, {})
    if not force and existing.get("indexed_at") and time.time() - float(existing["indexed_at"]) < STALE_SECONDS:
        return existing
    items: list[dict] = []
    seen: set[str] = set()
    old_by_path = {str(item.get("path")): item for item in existing.get("files", []) if isinstance(item, dict)}
    for root in workspace_roots(cfg):
        for path in _iter_code_files(root):
            sp = str(path)
            if sp in seen:
                continue
            seen.add(sp)
            try:
                st = path.stat()
                old = old_by_path.get(sp)
                if old and int(old.get("mtime", -1)) == int(st.st_mtime) and int(old.get("size", -1)) == int(st.st_size):
                    items.append(old)
                    if len(items) >= MAX_CODE_FILES:
                        break
                    continue
            except Exception:
                pass
            text = _read_doc(path)
            if not text:
                continue
            try:
                st = path.stat()
                items.append({
                    "path": sp,
                    "name": path.name,
                    "ext": path.suffix.lower(),
                    "mtime": int(st.st_mtime),
                    "size": int(st.st_size),
                    "symbols": _symbol_rows(path, text),
                    "imports": re.findall(r"^\s*(?:from\s+[\w.]+\s+import\s+[\w.*, ]+|import\s+[\w., ]+|require\(['\"][^'\"]+['\"]\))", text, flags=re.M)[:40],
                })
            except Exception:
                continue
            if len(items) >= MAX_CODE_FILES:
                break
        if len(items) >= MAX_CODE_FILES:
            break
    data = {"indexed_at": time.time(), "roots": [str(p) for p in workspace_roots(cfg)], "files": items}
    _write_json(CODE_INDEX, data)
    _write_code_card(data)
    return data


def _retrieve_code_index(query: str, cfg: dict | None = None, limit: int = 8) -> str:
    terms = _terms(query)
    if not terms:
        return ""
    data = refresh_code_index(cfg)
    hits = []
    for item in data.get("files", []):
        symbols = item.get("symbols", []) or []
        matching_symbols = [
            s for s in symbols
            if any(t in str(s.get("name", "")).lower() for t in terms)
        ]
        hay = " ".join([
            str(item.get("path", "")),
            str(item.get("name", "")),
            " ".join(str(s.get("name", "")) for s in symbols),
            " ".join(str(x) for x in item.get("imports", []) or []),
        ]).lower()
        score = sum(1 for t in terms if t in hay)
        score += len(matching_symbols) * 4
        if any(t in str(item.get("name", "")).lower() for t in terms):
            score += 3
        if score:
            hits.append((score, item, matching_symbols))
    if not hits:
        return ""
    hits.sort(key=lambda pair: (-pair[0], pair[1].get("path", "")))
    lines = ["Code index hits relevant to this question:", "Use these as pointers; read files or run tools before claiming exact behavior."]
    for _, item, matching_symbols in hits[:limit]:
        symbol_source = matching_symbols or (item.get("symbols") or [])[:12]
        syms = ", ".join(
            f"{s.get('name')}@{s.get('line')}" if s.get("line") else str(s.get("name"))
            for s in symbol_source
            if s.get("name")
        )
        lines.append(f"\nCODE: {item.get('path')}\nMATCHING_SYMBOLS: {syms}")
    return "\n".join(lines)[:5000]


def _run_probe(cmd: list[str], timeout: int = 6) -> dict:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"cmd": " ".join(cmd), "returncode": p.returncode, "stdout": (p.stdout or "")[-6000:], "stderr": (p.stderr or "")[-1200:]}
    except Exception as e:
        return {"cmd": " ".join(cmd), "error": str(e)[:300]}


def refresh_ops_index(cfg: dict | None = None, force: bool = False) -> dict:
    existing = _read_json(OPS_INDEX, {})
    if not force and existing.get("indexed_at") and time.time() - float(existing["indexed_at"]) < STALE_SECONDS:
        return existing
    probes = []
    if os.environ.get("JARVIS_DISABLE_OPS_INDEX", "").lower() not in {"1", "true", "yes"}:
        probes.extend([
            _run_probe(["hostname"]),
            _run_probe(["hostname", "-I"]),
            _run_probe(["systemctl", "list-timers", "--all", "--no-pager"], timeout=8),
            _run_probe(["systemctl", "list-units", "--type=service", "--all", "--no-pager"], timeout=8),
        ])
    data = {"indexed_at": time.time(), "roots": [str(p) for p in workspace_roots(cfg)], "probes": probes}
    _write_json(OPS_INDEX, data)
    _write_ops_card(data)
    return data


def _retrieve_ops_index(query: str, cfg: dict | None = None, limit: int = 6) -> str:
    terms = _terms(query)
    if not terms:
        return ""
    data = refresh_ops_index(cfg)
    hits = []
    for probe in data.get("probes", []):
        hay = " ".join(str(probe.get(k, "")) for k in ("cmd", "stdout", "stderr", "error")).lower()
        score = sum(1 for t in terms if t in hay)
        if score:
            hits.append((score, probe))
    if not hits:
        return ""
    hits.sort(key=lambda pair: -pair[0])
    lines = ["Operational index hits relevant to this question:", "These are cached probes; verify live for current status questions."]
    for _, probe in hits[:limit]:
        body = (probe.get("stdout") or probe.get("stderr") or probe.get("error") or "")[:900]
        lines.append(f"\nPROBE: {probe.get('cmd')}\nSNIPPET: {body}")
    return "\n".join(lines)[:5000]


def _write_code_card(data: dict) -> None:
    try:
        d = KNOW_DIR / "indexes"
        d.mkdir(parents=True, exist_ok=True)
        files = data.get("files", []) or []
        lines = [
            "# Codebase index",
            "",
            f"- Indexed at: {time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(float(data.get('indexed_at') or time.time())))}",
            f"- Files indexed: {len(files)}",
            "",
            "## Roots",
            "",
            *[f"- `{r}`" for r in (data.get("roots") or [])[:40]],
            "",
            "## Files and symbols",
            "",
        ]
        for item in files[:180]:
            syms = ", ".join(str(s.get("name")) for s in (item.get("symbols") or [])[:12] if s.get("name"))
            lines.append(f"- `{item.get('path')}`" + (f" | {syms}" if syms else ""))
        if len(files) > 180:
            lines.append(f"- ...and {len(files) - 180} more files.")
        (d / "CODEBASE_INDEX.md").write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    except Exception:
        pass


def _write_ops_card(data: dict) -> None:
    try:
        d = KNOW_DIR / "indexes"
        d.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Operational index",
            "",
            f"- Indexed at: {time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(float(data.get('indexed_at') or time.time())))}",
            "",
            "Cached operational probes. Jarvis should verify live before answering current status questions.",
            "",
        ]
        for probe in data.get("probes", []) or []:
            lines += [
                f"## `{probe.get('cmd')}`",
                "",
                "```text",
                (probe.get("stdout") or probe.get("stderr") or probe.get("error") or "")[:4000],
                "```",
                "",
            ]
        (d / "OPERATIONAL_INDEX.md").write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    except Exception:
        pass


def refresh_all_indexes(cfg: dict | None = None, force: bool = True) -> dict:
    docs = refresh_index(cfg, force=force)
    code = refresh_code_index(cfg, force=force)
    ops = refresh_ops_index(cfg, force=force)
    return {
        "ok": True,
        "docs": {"roots": len(docs.get("roots", [])), "files": len(docs.get("files", []))},
        "code": {"roots": len(code.get("roots", [])), "files": len(code.get("files", []))},
        "ops": {"probes": len(ops.get("probes", []))},
    }


def _retrieve_learned_memories(query: str, limit: int = 5) -> str:
    terms = _terms(query)
    if not terms:
        return ""
    query_ids = _memory_identifiers(query)
    hits = []
    try:
        if LEARNED.exists():
            for line in LEARNED.read_text(encoding="utf-8").splitlines()[-500:]:
                if not line.strip():
                    continue
                row = json.loads(line)
                fact = str(row.get("fact") or "")
                row_ids = _memory_identifiers(" ".join([fact, " ".join(str(k) for k in row.get("keywords", []) or [])]))
                if row_ids and not (query_ids & row_ids):
                    continue
                hay = " ".join([
                    fact,
                    " ".join(str(k) for k in row.get("keywords", []) or []),
                    str(row.get("scope") or ""),
                    str(row.get("source") or ""),
                ]).lower()
                score = sum(1 for t in terms if t in hay)
                if score:
                    hits.append((score, row))
    except Exception:
        pass
    if not hits:
        return ""
    hits.sort(key=lambda pair: -pair[0])
    lines = [
        "Learned memories relevant to this question:",
        "Treat these as hints, not truth. Prefer live/source evidence when a hint is volatile, stale, low-confidence, contradicted, or marked must_verify_before_answer.",
    ]
    seen = set()
    for _, row in hits[:limit]:
        fact = str(row.get("fact") or "").strip()
        if not fact or fact.lower() in seen:
            continue
        seen.add(fact.lower())
        trust = _row_trust_policy(row)
        volatility = _row_volatility(row)
        confidence = str(row.get("confidence") or "medium")
        stale = _memory_is_stale(row)
        verified_at = str(row.get("last_verified_at") or "")
        observed_at = str(row.get("last_observed_at") or row.get("ts") or "")
        kind = str(row.get("kind") or "stable_fact")
        lines.append(
            f"\nMEMORY: {fact}"
            f"\nKIND: {kind}"
            f"\nSCOPE: {row.get('scope', '')}"
            f"\nSOURCE: {row.get('source', '')}"
            f"\nTRUST_POLICY: {trust}"
            f"\nVOLATILITY: {volatility}"
            f"\nCONFIDENCE: {confidence}"
            f"\nSTALE: {'yes' if stale else 'no'}"
            f"\nOBSERVED: {observed_at}"
            f"\nVERIFIED: {verified_at}"
            f"\nLEARNED: {row.get('ts', '')}"
        )
    return "\n".join(lines)[:5000] if len(lines) > 2 else ""


def _retrieve_learning_gaps(query: str, limit: int = 4) -> str:
    terms = _terms(query)
    if not terms:
        return ""
    query_ids = _memory_identifiers(query)
    hits = []
    try:
        if LEARNING_DEBT.exists():
            for line in LEARNING_DEBT.read_text(encoding="utf-8").splitlines()[-300:]:
                if not line.strip():
                    continue
                row = json.loads(line)
                row_ids = _memory_identifiers(" ".join(str(row.get(k, "")) for k in ("question", "answer", "reason")))
                if row_ids and not (query_ids & row_ids):
                    continue
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
    wanted = _terms(question)
    try:
        if LEARNING_DEBT.exists():
            for line in LEARNING_DEBT.read_text(encoding="utf-8").splitlines()[-200:]:
                if not line.strip():
                    continue
                existing_row = json.loads(line)
                existing = _terms(str(existing_row.get("question") or ""))
                if question.strip().lower() == str(existing_row.get("question") or "").strip().lower():
                    queue_learning_question(question, reason)
                    return
                if wanted and existing and len(wanted & existing) >= max(3, min(len(wanted), len(existing)) // 2):
                    queue_learning_question(question, reason)
                    return
    except Exception:
        pass
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
                          keywords: list[str] | None = None, evidence: str = "",
                          kind: str | None = None, trust_policy: str | None = None,
                          volatility: str | None = None, confidence: str | None = None,
                          ttl_seconds: int | None = None, verified: bool = False) -> bool:
    """Persist a learned hint as both JSONL and an indexed markdown note.

    Memory is deliberately not stored as truth. New records carry trust metadata so a future
    turn can use them as orientation while still re-checking volatile or stale claims.
    """
    fact = (fact or "").strip()
    if len(fact) < 8:
        return False
    wanted = _terms(fact)
    wanted_ids = _memory_identifiers(fact)
    try:
        if LEARNED.exists():
            for line in LEARNED.read_text(encoding="utf-8").splitlines()[-200:]:
                if not line.strip():
                    continue
                row = json.loads(line)
                existing = _terms(str(row.get("fact") or ""))
                existing_ids = _memory_identifiers(str(row.get("fact") or ""))
                if fact.lower() == str(row.get("fact") or "").lower():
                    return False
                if (wanted_ids or existing_ids) and not (wanted_ids & existing_ids):
                    continue
                if wanted and existing and len(wanted & existing) >= max(3, min(len(wanted), len(existing)) // 2):
                    return False
    except Exception:
        pass
    KNOW_DIR.mkdir(parents=True, exist_ok=True)
    LEARNED_DIR.mkdir(parents=True, exist_ok=True)
    inferred = _infer_memory_metadata(
        fact,
        source=source,
        evidence=evidence,
        kind=kind,
        trust_policy=trust_policy,
        volatility=volatility,
        confidence=confidence,
        ttl_seconds=ttl_seconds,
        verified=verified,
    )
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    safe_evidence = _redact_evidence(evidence or "")[:2000]
    row = {
        "ts": now,
        "fact": fact[:2000],
        "source": source[:80],
        "scope": (scope or "project")[:40],
        "keywords": [str(k)[:80] for k in (keywords or [])[:20]],
        "evidence": safe_evidence,
        "evidence_ref": _evidence_ref(safe_evidence),
        "kind": inferred["kind"],
        "trust_policy": inferred["trust_policy"],
        "volatility": inferred["volatility"],
        "confidence": inferred["confidence"],
        "ttl_seconds": inferred["ttl_seconds"],
        "last_observed_at": now,
        "last_verified_at": now if verified else "",
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
        f"- Kind: {row['kind']}",
        f"- Trust policy: {row['trust_policy']}",
        f"- Volatility: {row['volatility']}",
        f"- Confidence: {row['confidence']}",
        f"- TTL seconds: {row['ttl_seconds']}",
        f"- Last observed at: {row['last_observed_at']}",
        f"- Last verified at: {row['last_verified_at'] or 'not verified'}",
        f"- Evidence ref: {row['evidence_ref']}",
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


def _infer_memory_metadata(fact: str, *, source: str = "", evidence: str = "",
                           kind: str | None = None, trust_policy: str | None = None,
                           volatility: str | None = None, confidence: str | None = None,
                           ttl_seconds: int | None = None, verified: bool = False) -> dict:
    text = " ".join([fact or "", source or "", evidence or ""]).lower()
    volatile = _looks_volatile(text)
    preference = _looks_like_preference(text, source)
    procedure = _looks_like_procedure(text)

    out_kind = (kind or "").strip() or (
        "volatile_status" if volatile else
        "preference" if preference else
        "procedure" if procedure else
        "stable_fact"
    )
    out_volatility = (volatility or "").strip() or ("volatile" if volatile else "stable")
    out_trust = (trust_policy or "").strip() or (
        "must_verify_before_answer" if volatile else
        "can_use_directly" if preference or procedure else
        "use_as_hint"
    )
    out_confidence = (confidence or "").strip() or ("high" if verified else "medium")
    if ttl_seconds is None:
        ttl_seconds = VOLATILE_TTL_SECONDS if out_volatility == "volatile" else DEFAULT_HINT_TTL_SECONDS
    return {
        "kind": _choice(out_kind, {"preference", "procedure", "stable_fact", "snapshot", "volatile_status", "warning", "gap"}, "stable_fact"),
        "volatility": _choice(out_volatility, {"stable", "snapshot", "volatile"}, "stable"),
        "trust_policy": _choice(out_trust, {"use_as_hint", "can_use_directly", "must_verify_before_answer"}, "use_as_hint"),
        "confidence": _choice(out_confidence, {"low", "medium", "high"}, "medium"),
        "ttl_seconds": max(0, int(ttl_seconds or 0)),
    }


def _choice(value: str, allowed: set[str], default: str) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in allowed else default


def _looks_volatile(text: str) -> bool:
    return any(s in text for s in (
        "currently", "right now", "today", "latest", "recent", "current status",
        "is running", "was running", "service is", "queue is", "queue count",
        "deployed", "not deployed", "fixed", "broken", "failing", "passing",
        "uptime", "load average", "price", "cost", "version", "release",
        "provider status", "account status", "ticket status",
    ))


def _looks_like_preference(text: str, source: str = "") -> bool:
    if str(source or "").startswith("owner"):
        return any(s in text for s in (
            "prefer", "preference", "likes", "wants", "always", "never",
            "do not", "don't", "next time", "remember that i", "remember this",
        ))
    return False


def _looks_like_procedure(text: str) -> bool:
    return any(s in text for s in (
        "runbook", "canonical", "use ", "command", "deploys from", "lives at",
        "repo", "repository", "path", "docs", "documentation", "workflow",
    ))


def _redact_evidence(evidence: str) -> str:
    text = evidence or ""
    patterns = (
        r"(?i)(api[_-]?key|token|secret|password|authorization|bearer)\s*[:=]\s*['\"]?[^'\"\s]+",
        r"(?i)bearer\s+[a-z0-9._~+/=-]{12,}",
        r"sk-[a-zA-Z0-9_-]{12,}",
    )
    for pat in patterns:
        text = re.sub(pat, "[redacted-secret]", text)
    return text


def _evidence_ref(evidence: str) -> str:
    if not evidence:
        return ""
    digest = hashlib.sha256(evidence.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"inline-redacted-sha256:{digest}"


def _row_trust_policy(row: dict) -> str:
    return _choice(str(row.get("trust_policy") or "use_as_hint"), {"use_as_hint", "can_use_directly", "must_verify_before_answer"}, "use_as_hint")


def _row_volatility(row: dict) -> str:
    return _choice(str(row.get("volatility") or "stable"), {"stable", "snapshot", "volatile"}, "stable")


def _memory_is_stale(row: dict) -> bool:
    try:
        ttl = int(row.get("ttl_seconds") or 0)
    except Exception:
        ttl = 0
    if ttl <= 0:
        return False
    raw = str(row.get("last_verified_at") or row.get("last_observed_at") or row.get("ts") or "")
    try:
        ts = time.mktime(time.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return True
    return time.time() - ts > ttl


def _memory_identifiers(text: str) -> set[str]:
    """Identifiers make similar facts distinct: ticket IDs, canaries, hostnames with digits, etc."""
    low = (text or "").lower()
    ids = set()
    for token in re.findall(r"\b[a-z][a-z0-9]+(?:-[a-z0-9]+)+\b", low):
        if any(ch.isdigit() for ch in token) or token.count("-") >= 2:
            ids.add(token)
    ids.update(re.findall(r"\b[a-z0-9._-]*\d[a-z0-9._-]*\b", low))
    return {x for x in ids if len(x) >= 4}


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
