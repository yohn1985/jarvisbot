#!/usr/bin/env python3
"""
Jarvis dashboard — a zero-dependency (stdlib) web view of what Jarvis is doing.

Serves a terminal-styled page with:
  - RUNS   : the kernel ticks + spawned workers (the swarm), live, with kill
  - CONFIG : the live config (mode, action classes, LLM routing, priorities)

    python jarvis/dashboard/server.py [--port 8787]

Open-source-clean: no infra-specific anything; reads only Jarvis's own state + config.
Meant to be the foundation an outsourced front-end can iterate on (the /api/* JSON is stable).
"""
from __future__ import annotations
import argparse, http.cookies, json, os, queue, secrets as _rand, signal, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
HTML = Path(__file__).resolve().parent / "dashboard.html"


def _dash_token():
    """Persistent access token for the dashboard (generated once). install.sh prints the URL with it."""
    f = ROOT / "state" / "dashboard_token"
    try:
        t = f.read_text().strip()
        if t:
            return t
    except Exception:
        pass
    t = _rand.token_urlsafe(24)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(t)
    try:
        os.chmod(f, 0o600)
    except Exception:
        pass
    return t


LOGIN_HTML = """<!doctype html><html><head><meta charset=utf-8><title>Jarvis</title><style>
body{background:#0c0f0d;color:#c8d6c4;font:14px ui-monospace,monospace;display:flex;height:100vh;margin:0;align-items:center;justify-content:center}
.b{border:1px solid #1d2a1d;border-radius:8px;padding:26px 30px;text-align:center;background:#11150f}
input{background:#0c100c;border:1px solid #1d2a1d;color:#c8d6c4;padding:8px 10px;border-radius:5px;font:inherit}
button{background:#3fae5a;color:#04140a;border:none;padding:8px 16px;border-radius:5px;cursor:pointer;margin-left:6px;font:inherit}
.hint{color:#6f7e6b;font-size:12px;margin-top:16px;line-height:1.6}
.hint code{background:#0c100c;border:1px solid #1d2a1d;border-radius:4px;padding:1px 6px;color:#7fe39a}
.beta{background:#d8a13a;color:#1a1206;font-size:9px;font-weight:700;letter-spacing:1.5px;padding:2px 6px;border-radius:4px;margin-left:8px}</style></head>
<body><div class=b><div style="color:#7fe39a;letter-spacing:2px;margin-bottom:14px">&#9679; JARVIS <span class=beta>EARLY BETA</span></div>
<div style="color:#6f7e6b;margin-bottom:12px">access token</div>
<form onsubmit="location='/?token='+encodeURIComponent(document.getElementById('t').value);return false">
<input id=t type=password autofocus placeholder="token"><button>enter</button></form>
<div class=hint>Don't have your token? On the machine running Jarvis, run:<br><code>./install.sh url</code><br>and open the link it prints (it embeds the token).</div></div></body></html>"""


def _runs():
    try:
        from jarvis.runtime import recent
        return recent(200)
    except Exception:
        return []


def _config():
    for name in ("config.yaml", "config.example.yaml"):
        p = ROOT / name
        if p.exists():
            try:
                import yaml
                return {"source": name, "config": yaml.safe_load(p.read_text())}
            except Exception:
                return {"source": name, "raw": p.read_text()}
    return {"source": None, "config": {}}


# --- dynamic model detection per pool (borrowed pattern from the ai-usage dashboard) ---
# Each backend in config.llm.backends is a "pool"; we list its models live where a key/CLI exists,
# falling back to a static set so the picker is never empty. Cached briefly (refresh=1 to bust).
# Comprehensive catalogs so a pool's picker is COMPLETE even with no API key (subscription-token
# users have no ANTHROPIC/OPENAI key to list from). Live API results override these when a key exists.
CLAUDE_MODELS_FALLBACK = ["claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5",
                          "claude-sonnet-4-6", "claude-sonnet-4-5", "claude-haiku-4-5", "claude-fable-5",
                          "opus", "sonnet", "haiku"]
CODEX_MODELS_FALLBACK = ["gpt-5.5", "gpt-5.5-codex", "gpt-5", "gpt-5-codex", "gpt-5.3-codex", "o4-mini"]
OPENAI_MODELS_FALLBACK = ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o3", "o4-mini"]
_POOL_FALLBACK = {"claude": CLAUDE_MODELS_FALLBACK, "anthropic": CLAUDE_MODELS_FALLBACK,
                  "codex": CODEX_MODELS_FALLBACK, "openai": OPENAI_MODELS_FALLBACK}
_POOLS_CACHE = {"ts": 0.0, "data": None}


def _http_get_json(url, headers, timeout=10):
    import urllib.request
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _anthropic_models(key):
    d = _http_get_json("https://api.anthropic.com/v1/models",
                       {"x-api-key": key, "anthropic-version": "2023-06-01"})
    return sorted([m["id"] for m in d.get("data", []) if "claude" in m.get("id", "")], reverse=True)


def _openai_style_models(url, key):
    d = _http_get_json(url, {"Authorization": f"Bearer {key}"} if key else {})
    return sorted([m["id"] for m in d.get("data", []) if m.get("id") and ":ft-" not in m["id"]])


def _detect_models(name, spec):
    """List a pool's models: live API when a key is present, else the known catalog. Never empty,
    never raises."""
    fb = _POOL_FALLBACK.get(name, [])
    try:
        if isinstance(spec, dict) and spec.get("http"):          # HTTP backend
            key = os.environ.get(spec.get("api_key_env", ""), "")
            if spec.get("format") == "anthropic":
                return (_anthropic_models(key) or fb or CLAUDE_MODELS_FALLBACK) if key else (fb or CLAUDE_MODELS_FALLBACK)
            if spec.get("api_key_env") and not key:
                return fb                                         # no key -> known catalog (never blank)
            return _openai_style_models(spec["http"].replace("chat/completions", "models"), key) or fb
        if name == "claude":                                     # CLI: uses subscription token; list via API key if any
            key = os.environ.get("ANTHROPIC_API_KEY", "")
            return (_anthropic_models(key) or fb) if key else fb
        if name == "codex":
            key = os.environ.get("OPENAI_API_KEY", "")
            return (_openai_style_models("https://api.openai.com/v1/models", key) or fb) if key else fb
        if name == "ollama":                                     # local CLI
            r = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=8)
            return [ln.split()[0] for ln in r.stdout.splitlines()[1:] if ln.strip()]
    except Exception:
        pass
    return fb


def _pools(refresh=False):
    """Pools (= configured backends) each with their detected models, for the routing pickers."""
    import time
    if not refresh and _POOLS_CACHE["data"] and time.time() - _POOLS_CACHE["ts"] < 120:
        return _POOLS_CACHE["data"]
    from jarvis.config import load
    from jarvis.adapters.llm import DEFAULT_BACKENDS
    llm = load().get("llm") or {}
    backends = dict(DEFAULT_BACKENDS)
    backends.update(llm.get("backends") or {})

    def _caps(n, s):
        # Reasoning capability comes from the backend KIND, not a model-name guess (so DeepSeek via
        # the OpenAI-style ollama_cloud HTTP backend correctly offers 'effort'). thinking = Anthropic.
        if n in ("claude", "anthropic") or (isinstance(s, dict) and s.get("format") == "anthropic"):
            return {"effort": False, "thinking": True}
        if n in ("codex", "openai", "ollama_cloud") or (isinstance(s, dict) and s.get("http")):
            return {"effort": True, "thinking": False}
        return {"effort": False, "thinking": False}

    pools = [{"name": n, "models": _detect_models(n, s), "caps": _caps(n, s)} for n, s in backends.items()]
    data = {"pools": pools, "aliases": llm.get("aliases") or {}}
    _POOLS_CACHE.update(ts=time.time(), data=data)
    return data


# Sections the owner is allowed to override from the CONFIG tab, and the validation each needs so a
# typo can't wedge the loop. Routing values aren't whitelisted (any backend:model is legal) but a
# blank is rejected so a role never loses its model.
_ACTION_VALUES = {"allow", "ask", "deny"}


def _write_yaml(path, data):
    """Atomically persist config (tmp + os.replace) so a concurrent reader/loop never sees a
    half-written config.yaml and a crash mid-write can't corrupt the file that drives the loop."""
    import yaml
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as f:
        f.write(yaml.safe_dump(data, sort_keys=False))
    os.replace(tmp, str(path))


def _save_config(patch):
    """Merge an owner edit from the CONFIG tab into config.yaml. Returns a human summary of what
    changed. Only known sections are touched; everything else in config.yaml is preserved."""
    import yaml
    p = ROOT / "config.yaml"
    data = (yaml.safe_load(p.read_text()) if p.exists() else {}) or {}
    changed = []
    if isinstance(patch.get("identity"), dict):
        idn = data.setdefault("identity", {})
        for k, v in patch["identity"].items():
            if v is not None and str(v).strip():
                idn[k] = str(v).strip(); changed.append(f"identity.{k}")
    if isinstance(patch.get("action_classes"), dict):
        ac = data.setdefault("action_classes", {})
        for k, v in patch["action_classes"].items():
            if v in _ACTION_VALUES:
                ac[k] = v; changed.append(f"action.{k}")
            elif v is not None:
                raise ValueError(f"action class '{k}' must be allow/ask/deny, got '{v}'")
    if isinstance(patch.get("routing"), dict):
        rt = data.setdefault("llm", {}).setdefault("routing", {})
        for k, v in patch["routing"].items():
            v = (v or "").strip()
            if not v:
                raise ValueError(f"role '{k}' can't be blank")
            if ":" not in v:
                raise ValueError(f"role '{k}' must look like 'backend:model', got '{v}'")
            rt[k] = v; changed.append(f"routing.{k}")
    if isinstance(patch.get("params"), dict):                # per-role reasoning controls (effort/thinking)
        pr = data.setdefault("llm", {}).setdefault("params", {})
        for role, rparams in patch["params"].items():
            if not isinstance(rparams, dict):
                continue
            slot = pr.setdefault(role, {})
            for k, allowed in (("effort", {"minimal", "low", "medium", "high"}),
                               ("thinking", {"low", "medium", "high"})):
                v = rparams.get(k)
                if v in (None, "", "default"):
                    slot.pop(k, None)
                elif v in allowed:
                    slot[k] = v
                else:
                    raise ValueError(f"{role}.{k} must be one of {sorted(allowed)} / default, got '{v}'")
            if not slot:
                pr.pop(role, None)
        if not pr:
            data.get("llm", {}).pop("params", None)
        changed.append("params")
    if isinstance(patch.get("priorities"), list):
        data["priorities"] = [s.strip() for s in patch["priorities"] if str(s).strip()]
        changed.append("priorities")
    _write_yaml(p, data)
    return "saved: " + (", ".join(changed) if changed else "nothing")


def _messages(conv=None):
    try:
        from jarvis import messaging
        return messaging.messages(conv)
    except Exception:
        return []


def _conversations():
    try:
        from jarvis import messaging
        return messaging.conversations()
    except Exception:
        return []


def _procs():
    """Live Jarvis-related processes (kernel + spawned workers)."""
    try:
        out = subprocess.run(["ps", "-eo", "pid,etime,cmd"], capture_output=True, text=True).stdout
    except Exception:
        return []
    procs = []
    for line in out.splitlines()[1:]:
        if "jarvis" in line and "dashboard/server.py" not in line and "grep" not in line:
            parts = line.split(None, 2)
            if len(parts) == 3:
                procs.append({"pid": parts[0], "etime": parts[1], "cmd": parts[2][:160]})
    return procs


_SKILL_INSTALLING = set()
_SKILL_ERRORS = {}        # name -> last install error (so a failed install is VISIBLE, not silent)


def _skills():
    """The shipped skills, each with install status so the dashboard can offer a one-click Install."""
    try:
        from jarvis.skills import list_skills
        from jarvis.bootstrap import installer
        installed_pkgs = installer._venv_installed()
        out = []
        for s in list_skills():
            req = Path(s.get("dir", "")) / "requirements.txt"
            has = installer._has_real_reqs(req)
            sat = (not has) or (installer._skill_satisfied(req, installed_pkgs) if installed_pkgs is not None else False)
            out.append({**s, "has_deps": has, "installed": sat,
                        "installing": s.get("name") in _SKILL_INSTALLING,
                        "install_error": _SKILL_ERRORS.get(s.get("name"))})
        return out
    except Exception:
        return []


def _install_skill(name):
    """Install a skill's deps into the venv in the background (one-click from the dashboard)."""
    if not name or name in _SKILL_INSTALLING:
        return {"started": False}
    _SKILL_INSTALLING.add(name)
    _SKILL_ERRORS.pop(name, None)

    def _run():
        try:
            p = subprocess.run([str(ROOT / "install.sh"), "skill", "install", name],
                               capture_output=True, text=True, timeout=300)
            if p.returncode != 0:            # capture the failure instead of swallowing it
                _SKILL_ERRORS[name] = ((p.stderr or p.stdout or "install failed").strip()[-300:])
        except Exception as e:
            _SKILL_ERRORS[name] = str(e)[:300]
        finally:
            _SKILL_INSTALLING.discard(name)

    threading.Thread(target=_run, daemon=True).start()
    return {"started": True}


def _discovery():
    """What Jarvis has written about its world. Each doc is tagged `primary` so the sidebar can show
    only the important ones (INDEX + per-host discovery docs); the many answered-question notes stay
    available (reachable via links in INDEX) but don't clutter the left rail."""
    items = []   # (path, primary)
    for d, primary in ((ROOT / "workspace" / "discovery", True), (ROOT / "workspace" / "knowledge", False)):
        if d.exists():
            items += [(p, primary) for p in d.glob("*.md")]
    # INDEX first, then primary discovery docs, then notes; newest within each group
    def order(it):
        p, primary = it
        group = 0 if p.name == "INDEX.md" else (1 if primary else 2)
        return (group, p.name)
    items.sort(key=order)
    out, seen = [], set()
    for p, primary in items:
        if p.name in seen:
            continue
        seen.add(p.name)
        try:
            out.append({"name": p.name, "content": p.read_text(), "primary": primary or p.name == "INDEX.md"})
        except Exception:
            pass
    return out[:60]


def _env_context(limit=4000):
    """The most recent discovery doc, so the brain actually KNOWS the environment it's in
    (connects autodiscovery to chat + thinking instead of leaving it in a folder)."""
    docs = _discovery()
    return docs[0]["content"][:limit] if docs else ""


UPLOADS = ROOT / "state" / "uploads"
_IMG_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}


def _save_uploads(images):
    """Persist owner-attached images (sent as data: URLs) under state/uploads. Returns metadata
    [{id,name,media_type,file}] stored on the message; the bytes are re-read for the vision model."""
    import base64, uuid as _uuid
    out = []
    for img in (images or [])[:10]:
        data = (img.get("data") if isinstance(img, dict) else img) or ""
        name = (img.get("name") if isinstance(img, dict) else "") or "image"
        if not data.startswith("data:"):
            continue
        head, _, b64 = data.partition(",")
        mt = head[5:].split(";")[0] or "image/png"
        if mt not in _IMG_EXT:
            continue
        try:
            raw = base64.b64decode(b64)
        except Exception:
            continue
        if len(raw) > 12 * 1024 * 1024:          # sanity cap (12MB)
            continue
        UPLOADS.mkdir(parents=True, exist_ok=True)
        iid = _uuid.uuid4().hex[:10]
        fn = f"{iid}.{_IMG_EXT[mt]}"
        (UPLOADS / fn).write_bytes(raw)
        out.append({"id": iid, "name": name[:80], "media_type": mt, "file": fn})
    return out


def _msg_image_paths(msg):
    """Existing on-disk paths for a message's attached images (for the vision model)."""
    paths = []
    for im in (msg or {}).get("images") or []:
        p = UPLOADS / os.path.basename(im.get("file", ""))
        if im.get("file") and p.exists():
            paths.append(str(p))
    return paths


def _web_skill():
    """Load the shipped web skill module so chat can browse the live web when the brain is unsure."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("jarvis_web_skill", str(ROOT / "skills" / "web" / "skill.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# --- bootstrap setup (the dashboard's guided checklist) ---
_EXEC = {"running": False, "last": None}
_EXEC_LOCK = threading.Lock()


def _enable_ollama_cloud():
    """Point the brain at Ollama Cloud (OpenAI-compatible HTTP) using the stored OLLAMA_API_KEY.
    deepseek-v4-pro for heavy thinking, deepseek-v4-flash for cheap/fast triage."""
    import yaml
    p = ROOT / "config.yaml"
    data = (yaml.safe_load(p.read_text()) if p.exists() else {}) or {}
    llm = data.setdefault("llm", {})
    llm.setdefault("backends", {})["ollama_cloud"] = {
        "http": "https://ollama.com/v1/chat/completions", "api_key_env": "OLLAMA_API_KEY"}
    flash, pro = "ollama_cloud:deepseek-v4-flash", "ollama_cloud:deepseek-v4-pro"
    llm["routing"] = {"triage": flash, "summarizer": flash, "researcher": pro,
                      "orchestrator": pro, "red_team": pro, "fixer": pro}
    llm["fallbacks"] = [flash]
    _write_yaml(p, data)


# How each shipped skill is invoked from a chat slash-command. The free text after the slash is
# passed as ONE argument (so multi-word questions survive without quoting). Skills not listed fall
# back to shell-splitting the args. argv is built relative to the skill's own dir at call time.
def _slash_argv(name, rest):
    rest = (rest or "").strip()
    if name == "web":
        return ["research", rest] if rest else None          # /web <question>
    if name == "deep-search":
        return [rest] if rest else None                       # /deep-search <question>
    if name == "discover":
        return ["--network"]                                  # /discover (rescan; concise one-liner)
    if name == "youtube-research":
        return ["search", rest] if rest else None             # /youtube-research <query>
    import shlex
    return shlex.split(rest) if rest else []                   # generic: flags/args


def _run_slash(conv, text):
    """Run a shipped skill from the chat as `/<skill> <args>` and post its output. Stdlib + the
    skill's own CLI; no brain needed (skills are mechanical), so this works even with no AI wired."""
    from jarvis import messaging
    parts = text[1:].split(None, 1)
    name = (parts[0] if parts else "").strip().lower()
    rest = parts[1] if len(parts) > 1 else ""
    skills = {s.get("name"): s for s in _skills()}
    if name in ("", "help", "skills", "?"):
        listing = "\n".join(f"  /{n}" for n in sorted(skills)) or "  (none installed)"
        messaging.reply(f"Skills you can run with `/`:\n{listing}\n\nExample: `/web is ubuntu 26 out`", conv=conv)
        return
    if name not in skills:
        messaging.reply(f"No skill `/{name}`. Try `/help`. Available: " + ", ".join("/" + n for n in sorted(skills)), conv=conv)
        return
    sk = skills[name]
    if sk.get("has_deps") and not sk.get("installed"):
        messaging.reply(f"`/{name}` needs to be installed first — open the **Skills** tab and click Install.", conv=conv)
        return
    argv = _slash_argv(name, rest)
    if argv is None:
        messaging.reply(f"`/{name}` needs some text after it, e.g. `/{name} <your question>`.", conv=conv)
        return
    skill_py = Path(sk["dir"]) / "skill.py"
    py = ROOT / ".venv" / "bin" / "python"
    py = str(py) if py.exists() else sys.executable
    # Stream the skill's output live so the owner SEES progress (network sweeps + model calls take
    # tens of seconds) instead of staring at a frozen chat until it finishes. PYTHONUNBUFFERED so
    # the child's progress lines arrive immediately; stderr is merged so step logs show too.
    mid = messaging.stream_start(conv=conv)
    buf = [f"running `/{name} {rest}`…\n".rstrip() + "\n"]
    messaging.stream_update(mid, "".join(buf))
    try:
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        p = subprocess.Popen([py, str(skill_py), *argv], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, cwd=str(ROOT), env=env, bufsize=1)
        start = time.time()
        for line in p.stdout:
            buf.append(line)
            messaging.stream_update(mid, ("".join(buf))[-6000:])
            if time.time() - start > 600:
                p.kill(); buf.append("\n(timed out)"); break
        try: p.wait(timeout=10)
        except Exception: pass
        out = ("".join(buf)).strip() or "(no output)"
    except Exception as e:
        out = ("".join(buf) + f"\n(failed: {str(e)[:200]})").strip()
    messaging.stream_end(mid, out[:6000])
    try:
        from jarvis.config import load as _load
        from jarvis import feedback
        feedback.record(_load(), kind="chat-skill", area=conv, summary=f"/{name} {rest}"[:120], outcome="ran")
    except Exception:
        pass


# --- live token push (SSE) ---------------------------------------------------------------------
# The background reply generator publishes deltas to a per-conversation _Stream; the SSE endpoint
# subscribes and forwards them to the browser token-by-token (no polling for the live reply).
_STREAMS = {}
_STREAMS_LOCK = threading.Lock()


class _Stream:
    def __init__(self):
        self.subs, self.think, self.text, self.status, self.done = [], "", "", None, False
        self.cancelled = False
        self.lock = threading.Lock()

    def emit(self, kind, data=""):
        with self.lock:
            if kind == "thinking":
                self.think += data
            elif kind == "text":
                self.text += data
            elif kind == "status":
                self.status = data
            elif kind == "done":
                self.done = True
                self.text = data or self.text
            for q in list(self.subs):
                try:
                    q.put_nowait((kind, data))
                except Exception:
                    pass

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            q.put(("set", {"thinking": self.think, "text": self.text, "status": self.status}))
            if self.done:
                q.put(("done", self.text))
            else:
                self.subs.append(q)
        return q


def _stream_open(conv):
    st = _Stream()
    with _STREAMS_LOCK:
        _STREAMS[conv] = st
    return st


def _stream_close(conv):
    def _later():                     # keep briefly so a late SSE subscriber still gets 'done'
        time.sleep(20)
        with _STREAMS_LOCK:
            if conv in _STREAMS and _STREAMS[conv].done:
                _STREAMS.pop(conv, None)
    threading.Thread(target=_later, daemon=True).start()


def _chat_reply(conv):
    """Generate Jarvis's reply to the latest owner message in a conversation, via the brain.
    Runs in a background thread so /api/say returns instantly; tokens push live over SSE and the
    message is also persisted so history/other clients see it."""
    try:
        from jarvis.config import load
        from jarvis.adapters.llm import build_llm
        from jarvis import messaging
        cfg = load()
        msgs = messaging.messages(conv, limit=20)
        last_msg = next((m for m in reversed(msgs)
                         if m.get("from") == "owner" and m.get("kind") in ("message", "note")), None)
        last = (last_msg or {}).get("text", "")
        img_paths = _msg_image_paths(last_msg)    # attached images -> shown to the vision model
        if last.strip().startswith("/"):          # chat slash-command -> run a skill directly
            _run_slash(conv, last.strip())
            return
        import time
        st = _STREAMS.get(conv) or _stream_open(conv)   # reuse the stream /api/say pre-opened (so the
        full = ""                                       # browser's EventSource attaches instantly)
        llm = build_llm(cfg)
        if not llm:
            nb = "(No AI brain is connected yet — open the Models tab to connect one.)"
            m0 = messaging.stream_start(conv); messaging.stream_end(m0, nb)
            st.emit("text", nb); st.emit("done", nb); _stream_close(conv)
            return
        transcript = "\n".join(("Owner: " if m.get("from") == "owner" else "Jarvis: ") + (m.get("text") or "")
                               for m in msgs if m.get("kind") in ("message", "note", "answer", "question"))
        ident = cfg.get("identity") or {}
        name = ident.get("name", "Jarvis")
        from jarvis import persona
        env = _env_context()
        base = (persona.system(cfg) + " You're chatting with the owner in your dashboard."
                + (f"\n\nWhat you've discovered about your environment:\n{env}\n" if env else "")
                + f"\nConversation so far:\n{transcript}\n")
        # 1) MULTI-TURN: a fast cheap-model decision on whether live web facts are needed; if so, post a
        #    visible status message and gather evidence before answering (Jarvis works out loud).
        web_ctx, used_web = "", False
        # Only spend a round-trip on the web-need decision when the message PLAUSIBLY needs live facts;
        # otherwise stream the answer immediately (no startup lag for ordinary questions).
        _hint = ("latest", "release", "version", "price", "news", "today", "current", "recent",
                 "2024", "2025", "2026", "http", "github", "docs", "weather", "who won", "stock")
        decide = "NO"
        if any(h in last.lower() for h in _hint):
            st.emit("status", "checking whether I need the web…")   # show activity during the triage call
            try:
                decide = llm.run("triage", base +
                    "\nDoes answering the latest owner message require CURRENT EXTERNAL web facts (software "
                    "releases, prices, news, third-party docs) that are NOT in the environment info above and "
                    "NOT about THIS machine? Reply EXACTLY 'SEARCH: <query>' if yes, otherwise 'NO'.",
                    timeout=60).strip()
            except Exception:
                decide = "NO"
        if decide.upper().startswith("SEARCH:"):
            query = decide.split(":", 1)[1].strip()[:160]
            st.emit("status", f"checking the web: {query}")
            try:
                ans, results = _web_skill().research(query, llm)
                src = "\n".join("- " + r.get("url", "") for r in (results or [])[:3])
                web_ctx, used_web = (ans + (f"\n\nSOURCES:\n{src}" if src else "")), True
            except Exception:
                web_ctx = ""
        # 2) STREAM the answer on the main brain so it appears as it's written — thinking streamed into
        #    its own collapsible block. Tokens push live over SSE; persisted (throttled) for history.
        mid = messaging.stream_start(conv)
        buf = {"t": "", "th": "", "last": 0.0}

        def on_delta(kind, d):
            if kind == "thinking":
                buf["th"] += d
            else:
                buf["t"] += d
            st.emit(kind, d)                          # per-token push to the browser (SSE)
            now = time.time()
            if now - buf["last"] > 0.5:               # persistence throttle (SSE is the live path)
                messaging.stream_update(mid, buf["t"], thinking=buf["th"]); buf["last"] = now

        prompt = (base + (f"\nLIVE WEB EVIDENCE:\n{web_ctx}\n" if web_ctx else "")
                  + ("\nThe owner attached image(s) below — examine them to answer." if img_paths else "")
                  + "\nAnswer the latest owner message. Be as CONCISE as possible: the SMALLEST answer "
                  "that fully conveys the essence — no preamble, filler, restating the question, or "
                  "sign-off. Prefer a sentence or two; expand only if genuinely needed. Use markdown; "
                  f"code in code blocks.\n\n{name}:")
        try:
            full = llm.run_stream("orchestrator", prompt, on_delta, timeout=200, think="medium",
                                  should_cancel=lambda: st.cancelled, images=img_paths or None).strip()
        except Exception:
            try:
                full = llm.run("orchestrator", prompt, timeout=200).strip()
            except Exception as e2:
                full = f"(couldn't reach my brain: {str(e2)[:120]})"
        if st.cancelled:
            full = (buf["t"].strip() + "  ⏹") if buf["t"].strip() else "⏹ stopped"
        messaging.stream_end(mid, full or buf["t"] or "(no reply)", thinking=buf["th"])
        st.emit("done", full)
        _stream_close(conv)
        try:
            from jarvis import feedback
            feedback.record(cfg, kind=("chat+web" if used_web else "chat"), area=conv,
                            summary=(msgs[-1].get("text", "")[:120] if msgs else ""), outcome="replied")
        except Exception:
            pass
    except Exception:
        # ALWAYS close the SSE even if we failed before the normal done — otherwise the browser's
        # EventSource hangs until its own timeout and the reply looks frozen.
        try:
            s = _STREAMS.get(conv)
            if s and not s.done:
                s.emit("done", "")
                _stream_close(conv)
        except Exception:
            pass


def _set_main_brain(target):
    """Point the MAIN reasoning (orchestrator + red_team) at a frontier backend, leaving the cheap
    agent roles as they are (e.g. DeepSeek). target like 'claude:opus'."""
    import yaml
    p = ROOT / "config.yaml"
    data = (yaml.safe_load(p.read_text()) if p.exists() else {}) or {}
    routing = data.setdefault("llm", {}).setdefault("routing", {})
    routing["orchestrator"] = target
    routing["red_team"] = target
    _write_yaml(p, data)


_MAIN_TARGET = {"openai": "openai:gpt-4o", "anthropic": "anthropic:claude-opus-4-8",
                "ollama": "ollama_cloud:deepseek-v4-pro",
                "claude_cli": "claude:opus", "codex_cli": "codex:gpt-5.5"}

# Frontier brains connected by pasting an API key (no terminal): HTTP endpoint + key env + model.
_FRONTIER = {
    "openai":    {"url": "https://api.openai.com/v1/chat/completions", "env": "OPENAI_API_KEY",
                  "model": "gpt-4o", "fmt": None},
    "anthropic": {"url": "https://api.anthropic.com/v1/messages", "env": "ANTHROPIC_API_KEY",
                  "model": "claude-opus-4-8", "fmt": "anthropic"},
}


def _add_frontier_backend(prov):
    """Add the provider's HTTP backend to config (does NOT change the main brain — that's gated on a
    successful probe)."""
    import yaml
    c = _FRONTIER[prov]
    p = ROOT / "config.yaml"
    data = (yaml.safe_load(p.read_text()) if p.exists() else {}) or {}
    llm = data.setdefault("llm", {})
    spec = {"http": c["url"], "api_key_env": c["env"]}
    if c["fmt"]:
        spec["format"] = c["fmt"]
    llm.setdefault("backends", {})[prov] = spec
    _write_yaml(p, data)


def _probe_target(cfg, target):
    """Probe a SPECIFIC backend:model (no fallback) so we only switch the main brain to one that
    actually answers. Returns (ok, detail)."""
    from jarvis.adapters.llm import build_llm
    llm = build_llm(cfg)
    if not llm:
        return False, "no router"
    backend, _, model = target.partition(":")
    model = llm.aliases.get(model, model)
    if not llm._present(backend):
        return False, f"backend '{backend}' unavailable"
    try:
        out = llm._invoke(backend, model, "Reply with exactly: OK", 45)
        return ("ok" in (out or "").lower()), (out or "")[:180]
    except Exception as e:
        return False, str(e)[:200]


def _verify_main(cfg):
    """Probe the MAIN brain (orchestrator) specifically with NO fallback, so a bad key/model fails
    loudly instead of a cheap fallback masking it."""
    from jarvis.adapters.llm import build_llm
    llm = build_llm(cfg)
    if not llm:
        return False, "no brain configured"
    llm.fallbacks = []                      # verify the actual main backend, not a fallback
    try:
        out = llm.run("orchestrator", "Reply with exactly: OK", timeout=45)
        return ("ok" in (out or "").lower()), (out or "")[:140]
    except Exception as e:
        return False, str(e)[:180]


def _models():
    """MODELS cards in two groups: subscription login (CLI, cheapest) and API key (pay per use)."""
    cfg = {}
    try:
        from jarvis.config import load
        cfg = load()
    except Exception:
        pass
    main = ((cfg.get("llm", {}) or {}).get("routing", {}) or {}).get("orchestrator", "")
    home = Path(os.path.expanduser("~"))
    SUB, KEY = "Subscription login (cheapest)", "API key (pay per use)"
    return [
        {"id": "claude_cli", "name": "Claude — Max/Pro plan", "section": SUB, "kind": "cli",
         "cmd": "claude setup-token", "field": "token",
         "connected": bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or (home / ".claude" / ".credentials.json").exists()),
         "is_main": main.startswith("claude:"),
         "note": "One-click login is coming. For now: on a computer where you use Claude, run this — it opens your browser, logs in, and prints a token to paste below."},
        {"id": "codex_cli", "name": "Codex — ChatGPT plan", "section": SUB, "kind": "cli",
         "cmd": "codex login", "field": "token",
         "connected": (home / ".codex" / "auth.json").exists(), "is_main": main.startswith("codex"),
         "note": "One-click login is coming. For now: on a computer with a browser, run this to log in, then paste the access token below."},
        {"id": "ollama", "name": "Ollama Cloud — cheap, recommended", "section": KEY, "kind": "key",
         "field": "key", "get": "https://ollama.com/settings/keys",
         "connected": bool(os.environ.get("OLLAMA_API_KEY")), "is_main": main.startswith("ollama")},
        {"id": "openai", "name": "OpenAI (GPT)", "section": KEY, "kind": "key", "field": "key",
         "get": "https://platform.openai.com/api-keys",
         "connected": bool(os.environ.get("OPENAI_API_KEY")), "is_main": main.startswith("openai")},
        {"id": "anthropic", "name": "Claude (Anthropic)", "section": KEY, "kind": "key", "field": "key",
         "get": "https://console.anthropic.com/settings/keys",
         "connected": bool(os.environ.get("ANTHROPIC_API_KEY")), "is_main": main.startswith("anthropic")},
    ]


def _setup_state():
    """One simple snapshot the UI uses to show the next thing for the owner to do."""
    try:
        from jarvis.config import load
        from jarvis.bootstrap import preflight, installer
        cfg = load()
        st = preflight.status(cfg)
        plan = installer.plan(cfg)
        # "Your turn" = unmet required checks the install plan can't fix itself (e.g. logging the
        # brain in). Skip the brain item while a CLI install is still queued — that's not on you yet.
        installing_cli = any(a["id"].startswith("cli_") for a in plan)
        needs_user = [{"key": c["key"], "desc": c.get("hint") or c["label"]}
                      for c in st["checks"] if c["required"] and not c["ok"]
                      and not (c["key"] == "brain" and installing_cli)]
        return {"ready": st["ready"], "checks": st["checks"], "plan": plan, "needs_user": needs_user,
                "running": _EXEC["running"], "last": _EXEC["last"],
                "ollama_key_set": bool(os.environ.get("OLLAMA_API_KEY")),
                "complete": st["ready"] and not plan}
    except Exception as e:
        return {"error": str(e)[:200], "checks": [], "plan": [], "running": False, "complete": False}


def _approve(ids):
    """Run the approved install actions in a background thread so the request returns at once;
    progress streams into the RUNS feed the page already polls."""
    with _EXEC_LOCK:
        if _EXEC["running"]:
            return {"started": False, "error": "install already running"}
        _EXEC["running"] = True

    def _run():
        try:
            from jarvis.config import load
            from jarvis.bootstrap import executor
            _EXEC["last"] = executor.execute(load(), approved_ids=ids, interactive=False)
        except Exception as e:
            _EXEC["last"] = {"error": str(e)[:200]}
        finally:
            _EXEC["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return {"started": True}


class H(BaseHTTPRequestHandler):
    def _chat_stream(self, conv):
        """SSE: subscribe to the in-progress reply for `conv` and push deltas to the browser live."""
        st = None
        for _ in range(60):                          # wait up to ~6s for generation to register (say->SSE race)
            with _STREAMS_LOCK:
                st = _STREAMS.get(conv)
            if st:
                break
            time.sleep(0.1)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except Exception:
            return

        def w(ev, data):
            try:
                self.wfile.write(f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()
                return True
            except Exception:
                return False

        if not st:
            w("done", {}); return
        q = st.subscribe()
        while True:
            try:
                kind, data = q.get(timeout=20)
            except Exception:
                if not w("ping", {}):
                    break
                continue
            if kind == "set":
                if not w("set", data):
                    break
            elif kind == "status":
                if not w("status", {"text": data}):
                    break
            elif kind in ("thinking", "text"):
                if not w(kind, {"d": data}):
                    break
            elif kind == "done":
                w("done", {"text": data}); break

    def _send(self, code, body, ctype="application/json", set_cookie=None):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(b)

    def _authed(self, u):
        """'query' if a valid ?token=, 'cookie' if a valid jarvis_token cookie, else None."""
        tok = _dash_token()
        if (parse_qs(u.query).get("token") or [None])[0] == tok:
            return "query"
        c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        if c.get("jarvis_token") and c["jarvis_token"].value == tok:
            return "cookie"
        return None

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        u = urlparse(self.path)
        auth = self._authed(u)
        if not auth:
            if u.path.startswith("/api/"):
                return self._send(401, json.dumps({"error": "unauthorized"}))
            return self._send(200, LOGIN_HTML, "text/html")
        if u.path in ("/", "/index.html"):
            ck = (f"jarvis_token={_dash_token()}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000"
                  if auth == "query" else None)
            return self._send(200, HTML.read_text() if HTML.exists() else "<h1>Jarvis</h1>",
                              "text/html", set_cookie=ck)
        if u.path == "/api/runs":
            return self._send(200, json.dumps(_runs()))
        if u.path == "/api/config":
            return self._send(200, json.dumps(_config()))
        if u.path == "/api/pools":
            refresh = parse_qs(u.query).get("refresh", ["0"])[0] not in ("0", "", "false")
            return self._send(200, json.dumps(_pools(refresh=refresh)))
        if u.path == "/api/procs":
            return self._send(200, json.dumps(_procs()))
        if u.path == "/api/setup":
            return self._send(200, json.dumps(_setup_state()))
        if u.path == "/api/discovery":
            return self._send(200, json.dumps(_discovery()))
        if u.path == "/api/skills":
            return self._send(200, json.dumps(_skills()))
        if u.path == "/api/models":
            return self._send(200, json.dumps(_models()))
        if u.path == "/api/conversations":
            return self._send(200, json.dumps(_conversations()))
        if u.path == "/api/messages":
            conv = (parse_qs(u.query).get("conv") or [None])[0]
            return self._send(200, json.dumps(_messages(conv)))
        if u.path == "/api/chat-stream":              # Server-Sent Events: live token push for a reply
            return self._chat_stream(parse_qs(u.query).get("conv", ["general"])[0])
        if u.path == "/api/upload":                   # serve an attached image
            fp = UPLOADS / os.path.basename((parse_qs(u.query).get("f") or [""])[0])
            if fp.exists() and fp.is_file() and fp.parent == UPLOADS:
                ct = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif",
                      "webp": "image/webp"}.get(fp.suffix.lstrip("."), "application/octet-stream")
                return self._send(200, fp.read_bytes(), ct)
            return self._send(404, json.dumps({"error": "not found"}))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        u = urlparse(self.path)
        if not self._authed(u):
            return self._send(401, json.dumps({"error": "unauthorized"}))
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        if u.path == "/api/alert":
            # Alertmanager (or anyone) POSTs here to wake Jarvis immediately.
            (ROOT / "state").mkdir(exist_ok=True)
            (ROOT / "state" / "wake").touch()
            return self._send(200, json.dumps({"ok": True, "woke": True}))
        if u.path == "/api/approve":
            ids = body.get("ids")
            return self._send(200, json.dumps(_approve(ids)))
        if u.path == "/api/skill-install":
            return self._send(200, json.dumps(_install_skill((body.get("name") or "").strip())))
        if u.path == "/api/set-main":
            target = _MAIN_TARGET.get((body.get("provider") or "").strip())
            if not target:
                return self._send(400, json.dumps({"ok": False, "error": "unknown provider"}))
            try:
                from jarvis.config import load
                from jarvis.bootstrap import preflight
                _set_main_brain(target)
                ok, detail = preflight.recheck_brain(load())
                return self._send(200, json.dumps({"ok": ok, "detail": detail}))
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)[:200]}))
        if u.path == "/api/verify-brain":
            try:
                from jarvis.config import load
                from jarvis.bootstrap import preflight
                ok, detail = preflight.recheck_brain(load())
                return self._send(200, json.dumps({"ok": ok, "detail": detail}))
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)[:200]}))
        if u.path == "/api/brain-key":
            prov, key = (body.get("provider") or "").strip(), (body.get("key") or "").strip()
            try:
                from jarvis.bootstrap import secrets
                from jarvis.config import load
                from jarvis.bootstrap import preflight
                if prov == "claude_cli":                   # subscription token -> probe BEFORE switching
                    if not key:
                        return self._send(400, json.dumps({"ok": False, "error": "no token provided"}))
                    secrets.set_secret("CLAUDE_CODE_OAUTH_TOKEN", key)
                    secrets.load_env()
                    ok, detail = _probe_target(load(), "claude:opus")
                    if ok:
                        _set_main_brain("claude:opus")
                        return self._send(200, json.dumps({"ok": True, "detail": "Connected — Claude is now the main brain."}))
                    secrets.unset("CLAUDE_CODE_OAUTH_TOKEN")     # don't keep a token that doesn't work
                    return self._send(200, json.dumps({"ok": False, "detail": f"That token didn't work — Claude said: {detail}. Paste a fresh `claude setup-token`."}))
                if prov == "codex_cli":
                    import shutil as _sh, subprocess as _sp
                    if not key:
                        return self._send(400, json.dumps({"ok": False, "error": "no token provided"}))
                    if not _sh.which("codex"):
                        return self._send(400, json.dumps({"ok": False, "error": "codex CLI isn't installed on this machine yet"}))
                    try:
                        _sp.run(["codex", "login", "--with-access-token"], input=key, capture_output=True, text=True, timeout=30)
                    except Exception as e:
                        return self._send(500, json.dumps({"ok": False, "error": str(e)[:160]}))
                    ok, detail = _probe_target(load(), "codex:gpt-5.5")
                    if ok:
                        _set_main_brain("codex:gpt-5.5")
                        return self._send(200, json.dumps({"ok": True, "detail": "Connected — Codex is now the main brain."}))
                    return self._send(200, json.dumps({"ok": False, "detail": f"Logged in but no usable reply: {detail}"}))
                if prov in _FRONTIER:                       # API key -> probe BEFORE switching
                    if not key:
                        return self._send(400, json.dumps({"ok": False, "error": "no key provided"}))
                    secrets.set_secret(_FRONTIER[prov]["env"], key)
                    secrets.load_env()
                    _add_frontier_backend(prov)
                    ok, detail = _probe_target(load(), _MAIN_TARGET[prov])
                    if ok:
                        _set_main_brain(_MAIN_TARGET[prov])
                        return self._send(200, json.dumps({"ok": True, "detail": f"Connected — {prov} is now the main brain."}))
                    secrets.unset(_FRONTIER[prov]["env"])         # don't keep a key that doesn't work
                    return self._send(200, json.dumps({"ok": False, "detail": f"That key didn't work: {detail}"}))
                if prov == "ollama":
                    if key:
                        secrets.set_secret("OLLAMA_API_KEY", key)
                    secrets.load_env()
                    if not os.environ.get("OLLAMA_API_KEY"):
                        return self._send(400, json.dumps({"ok": False, "error": "no Ollama key stored yet"}))
                    _enable_ollama_cloud()                 # agents tier + (until a frontier is set) main
                    ok, detail = preflight.recheck_brain(load())
                    return self._send(200, json.dumps({"ok": ok, "detail": detail}))
                if not key:
                    return self._send(400, json.dumps({"ok": False, "error": "no key provided"}))
                env_name = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(prov, prov.upper() + "_API_KEY")
                secrets.set_secret(env_name, key)
                return self._send(200, json.dumps({"ok": True, "stored": env_name}))
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)[:200]}))
        if u.path == "/api/kill":            # POST (not GET): a GET here was a CSRF-to-process-kill,
            pid = str(body.get("pid", ""))   # since a SameSite=Lax cookie rides a top-level GET navigation
            try:
                os.kill(int(pid), signal.SIGTERM)
                return self._send(200, json.dumps({"ok": True, "pid": pid}))
            except Exception as e:
                return self._send(400, json.dumps({"ok": False, "error": str(e)}))
        try:
            from jarvis import messaging
            if u.path == "/api/answer":
                ok = messaging.answer(body.get("id", ""), body.get("text", ""))
                return self._send(200 if ok else 400, json.dumps({"ok": ok}))
            if u.path == "/api/say":
                conv = body.get("conv", "general")
                # STEERING: if a reply is mid-flight, interrupt it; the new reply will incorporate this
                # message (and the partial it already wrote) so the owner can redirect on the fly.
                with _STREAMS_LOCK:
                    cur = _STREAMS.get(conv)
                if cur and not cur.done:
                    cur.cancelled = True
                    for _ in range(30):                  # let the old generation wind down (keep its partial)
                        if cur.done:
                            break
                        time.sleep(0.1)
                imgs = _save_uploads(body.get("images"))
                text = body.get("text", "")
                messaging.say(text, conv=conv, images=imgs)
                if not text.strip().startswith("/"):     # open the SSE stream NOW (not deep in the thread)
                    _stream_open(conv)                   # so the browser's EventSource attaches with no 6s gap
                threading.Thread(target=_chat_reply, args=(conv,), daemon=True).start()
                return self._send(200, json.dumps({"ok": True}))
            if u.path == "/api/stop":                    # interrupt the in-progress reply
                conv = body.get("conv", "general")
                with _STREAMS_LOCK:
                    cur = _STREAMS.get(conv)
                if cur and not cur.done:
                    cur.cancelled = True
                return self._send(200, json.dumps({"ok": True}))
            if u.path == "/api/archive":
                messaging.archive(body.get("conv", ""), bool(body.get("archived", True)))
                return self._send(200, json.dumps({"ok": True}))
            if u.path == "/api/config-save":
                try:
                    detail = _save_config(body or {})
                    return self._send(200, json.dumps({"ok": True, "detail": detail}))
                except Exception as e:
                    return self._send(500, json.dumps({"ok": False, "error": str(e)[:200]}))
        except Exception as e:
            return self._send(500, json.dumps({"ok": False, "error": str(e)}))
        return self._send(404, json.dumps({"error": "not found"}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print(f"[jarvis-dashboard] http://{a.host}:{a.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
