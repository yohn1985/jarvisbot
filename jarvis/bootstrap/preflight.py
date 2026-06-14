"""Pure-Python preflight (Phase 0) — runs with NO AI brain.

The loop calls preflight.run(cfg) FIRST every wake. It deterministically checks the
prerequisites Jarvis needs and, for anything missing, posts a plain templated question to the
dashboard chat. No LLM is ever involved — this is the chicken-and-egg fix: Jarvis can't think
until it has a brain, so plain Python notices what's missing and asks the owner for it.

It is idempotent: it remembers what it already asked (state/setup.json) so it never spams the
same question, and it auto-closes a question once the requirement is satisfied.

run() -> {"ready": bool, "checks": [...], "asked": [...], "resolved": [...]}.
'ready' is True only when every REQUIRED check passes (today: a usable AI CLI).

Stdlib only.
"""
from __future__ import annotations
import json, os, shutil, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
STATE = ROOT / "state"
SETUP_STATE = STATE / "setup.json"

# Canonical CLI binary per backend, used when config doesn't spell it out.
_DEFAULT_CLIS = {"claude": "claude", "codex": "codex", "ollama": "ollama"}

# Where AI CLIs commonly live beyond $PATH. Critical because under systemd the loop's PATH is
# often just /usr/bin:/bin, so a CLI installed in ~/.local/bin or an npm global dir is invisible
# to a naive `which`. We search these too, then wire the hit back onto PATH (see _ensure_on_path).
_COMMON_BIN_DIRS = [
    "~/.local/bin", "~/bin", "/usr/local/bin", "/usr/bin", "/bin", "/snap/bin",
    "~/.npm-global/bin", "/usr/local/lib/node_modules/.bin", "/opt/homebrew/bin",
]


def _find_binary(name: str) -> str | None:
    """Find an executable by PATH first, then common install locations (incl. nvm node dirs)."""
    hit = shutil.which(name)
    if hit:
        return hit
    for d in _COMMON_BIN_DIRS:
        cand = Path(os.path.expanduser(d)) / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    nvm = Path(os.path.expanduser("~/.nvm/versions/node"))
    if nvm.exists():
        for cand in sorted(nvm.glob(f"*/bin/{name}"), reverse=True):
            if os.access(cand, os.X_OK):
                return str(cand)
    return None


def _ai_clis(cfg: dict) -> dict:
    """{backend_name: cli_binary} from config.llm.backends, falling back to the known CLIs."""
    backends = (cfg.get("llm", {}) or {}).get("backends") or {}
    out = {name: cmd[0] for name, cmd in backends.items()
           if isinstance(cmd, (list, tuple)) and cmd}
    return out or dict(_DEFAULT_CLIS)


def _ensure_on_path(paths) -> None:
    """Prepend the directories of discovered CLIs to this process's PATH, so the rest of Jarvis
    (the LLM router uses shutil.which) can run a CLI we found outside the default PATH."""
    cur = os.environ.get("PATH", "").split(os.pathsep)
    add = [d for d in (str(Path(p).resolve().parent) for p in paths) if d and d not in cur]
    if add:
        os.environ["PATH"] = os.pathsep.join(add + cur)


def find_ai_clis(cfg: dict) -> dict:
    """{backend: absolute_path} for every configured AI CLI actually present on this machine."""
    return {b: p for b, bin_ in _ai_clis(cfg).items() for p in [_find_binary(bin_)] if p}


# --- requirement checks (return (ok, detail); no AI) ---

def check_ai_brain(cfg: dict):
    found = find_ai_clis(cfg)
    if found:
        _ensure_on_path(found.values())   # make what we found usable for the rest of the run
        return True, "found " + ", ".join(f"{b}={p}" for b, p in sorted(found.items()))
    looked = ", ".join(sorted(_ai_clis(cfg).values()))
    return False, f"no AI CLI found on PATH or common dirs (looked for: {looked})"


def _ask_ai_brain(cfg: dict) -> str:
    names = " or ".join(f"`{b} login`" for b in sorted(_ai_clis(cfg).keys()) if b != "ollama") \
        or "`claude login`"
    return (
        "I don't have an AI brain yet — I can't find an AI CLI on this machine, so I can't "
        "think or plan until you give me one.\n\n"
        "On this PC, do ONE of these:\n"
        f"  - run {names}   (recommended — the CLI keeps the login token; I store nothing)\n"
        "  - or reply here with an API key and I'll keep it in my local vault.\n\n"
        "I search PATH and the usual install spots, so once it's set up I'll detect it on my "
        "next check and tell you here."
    )


# (key, label, required, check_fn, ask_fn)
REQUIREMENTS = [
    ("ai_brain", "AI brain (claude/codex CLI)", True, check_ai_brain, _ask_ai_brain),
]


# --- idempotent ask bookkeeping (state/setup.json) ---

def _load_state() -> dict:
    if SETUP_STATE.exists():
        try:
            return json.loads(SETUP_STATE.read_text())
        except Exception:
            return {}
    return {}


def _save_state(s: dict) -> None:
    STATE.mkdir(exist_ok=True)
    SETUP_STATE.write_text(json.dumps(s, indent=2))


def _ask(key: str, text: str, st: dict) -> bool:
    """Post the question once. Returns True only if a NEW question was posted."""
    rec = st.get(key) or {}
    if rec.get("qid") and not rec.get("resolved"):
        return False                      # already asked and still open — don't spam
    try:
        from jarvis import messaging
        row = messaging.post_question(text, ref=f"setup:{key}", conv="setup", title="Setup")
    except Exception:
        return False
    st[key] = {"qid": row["id"], "asked_ts": row["ts"], "resolved": False}
    return True


def _resolve(key: str, label: str, st: dict) -> bool:
    """Close a previously-asked question once its requirement is satisfied."""
    rec = st.get(key) or {}
    if not rec.get("qid") or rec.get("resolved"):
        return False
    try:
        # NOTE: messaging.answer() rewrites messages.jsonl; the loop is the only writer here, but
        # this shares the known loop/dashboard write race — to be hardened in the robustness step.
        from jarvis import messaging
        messaging.answer(rec["qid"], f"resolved - {label} is now available.")
    except Exception:
        pass
    rec["resolved"] = True
    st[key] = rec
    return True


def run(cfg: dict) -> dict:
    st = _load_state()
    checks, asked, resolved, ready = [], [], [], True
    for key, label, required, check_fn, ask_fn in REQUIREMENTS:
        try:
            ok, detail = check_fn(cfg)
        except Exception as e:
            ok, detail = False, f"check error: {str(e)[:80]}"
        checks.append({"key": key, "label": label, "required": required, "ok": ok, "detail": detail})
        if ok:
            if _resolve(key, label, st):
                resolved.append(key)
        else:
            if required:
                ready = False
            if _ask(key, ask_fn(cfg), st):
                asked.append(key)
    _save_state(st)
    return {"ready": ready, "checks": checks, "asked": asked, "resolved": resolved}
