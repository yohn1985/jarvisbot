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

_VERIFY_TTL = 20   # seconds — while a brain isn't verified yet, re-probe at most this often


def _probe_brain(cfg: dict):
    """A tiny REAL call to confirm the brain actually ANSWERS. Fast-fails (and free) when the CLI
    is installed but not logged in. This is the one place setup talks to the LLM — it's a ping,
    not thinking."""
    try:
        from jarvis.adapters.llm import build_llm
        llm = build_llm(cfg)
        if not llm:
            return False, "no LLM router configured"
        out = (llm.run("triage", "Reply with exactly: OK", timeout=30) or "").strip()
    except Exception as e:
        return False, f"brain not responding ({str(e)[:60]})"
    return ("ok" in out.lower()), ("brain answered" if "ok" in out.lower() else "brain gave no usable reply")


def brain_installed(cfg: dict) -> bool:
    return bool(find_ai_clis(cfg))


def check_brain(cfg: dict):
    """Required: the brain is installed AND actually answers. The successful probe is cached in
    state (so we don't call the LLM every wake); while unverified we re-probe at most every
    _VERIFY_TTL seconds. No CLI -> not ok, but we do NOT probe (keeps the no-AI bootstrap clean)."""
    found = find_ai_clis(cfg)
    if found:
        _ensure_on_path(found.values())
    else:
        return False, "no AI CLI installed yet"
    st = _load_state()
    b = st.get("brain") or {}
    if b.get("verified"):
        return True, "brain verified"
    if time.time() - float(b.get("last_probe", 0)) < _VERIFY_TTL:
        return False, b.get("detail", "verifying…")
    ok, detail = _probe_brain(cfg)
    b["last_probe"], b["detail"] = time.time(), detail
    if ok:
        b["verified"] = True
    st["brain"] = b
    _save_state(st)
    return ok, detail


def _ask_brain(cfg: dict) -> str:
    if not find_ai_clis(cfg):
        return ("I don't have an AI brain yet. Open the SETUP tab and approve the install — or "
                "install a CLI and run `claude login` — so I can think.")
    return ("My AI brain is installed but not logged in, so I still can't think. On this machine, "
            "run `claude login` (or `codex login`) in a terminal — or paste an API key here and "
            "I'll keep it in my local vault.")


# (key, label, required, check_fn, ask_fn)
REQUIREMENTS = [
    ("brain", "AI brain (responds)", True, check_brain, _ask_brain),
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


def status(cfg: dict) -> dict:
    """Read-only snapshot of the requirement checks — no asking, no state writes. For the
    dashboard setup checklist."""
    checks, ready = [], True
    for key, label, required, check_fn, ask_fn in REQUIREMENTS:
        try:
            ok, detail = check_fn(cfg)
        except Exception as e:
            ok, detail = False, f"check error: {str(e)[:80]}"
        item = {"key": key, "label": label, "required": required, "ok": ok, "detail": detail}
        if not ok:
            try:
                item["hint"] = ask_fn(cfg)
            except Exception:
                item["hint"] = ""
        checks.append(item)
        if required and not ok:
            ready = False
    return {"ready": ready, "checks": checks}


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
