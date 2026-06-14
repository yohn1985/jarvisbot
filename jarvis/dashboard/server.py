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
import argparse, http.cookies, json, os, secrets as _rand, signal, subprocess, sys, threading
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
button{background:#3fae5a;color:#04140a;border:none;padding:8px 16px;border-radius:5px;cursor:pointer;margin-left:6px;font:inherit}</style></head>
<body><div class=b><div style="color:#7fe39a;letter-spacing:2px;margin-bottom:14px">&#9679; JARVIS</div>
<div style="color:#6f7e6b;margin-bottom:12px">access token</div>
<form onsubmit="location='/?token='+encodeURIComponent(document.getElementById('t').value);return false">
<input id=t type=password autofocus placeholder="token"><button>enter</button></form></div></body></html>"""


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
            out.append({**s, "has_deps": has, "installed": sat, "installing": s.get("name") in _SKILL_INSTALLING})
        return out
    except Exception:
        return []


def _install_skill(name):
    """Install a skill's deps into the venv in the background (one-click from the dashboard)."""
    if not name or name in _SKILL_INSTALLING:
        return {"started": False}
    _SKILL_INSTALLING.add(name)

    def _run():
        try:
            subprocess.run([str(ROOT / "install.sh"), "skill", "install", name],
                           capture_output=True, text=True, timeout=300)
        except Exception:
            pass
        finally:
            _SKILL_INSTALLING.discard(name)

    threading.Thread(target=_run, daemon=True).start()
    return {"started": True}


def _discovery():
    """What Jarvis has written about its world: discovery docs + answered-knowledge notes (INDEX first)."""
    paths = []
    for d in (ROOT / "workspace" / "discovery", ROOT / "workspace" / "knowledge"):
        if d.exists():
            paths += [p for p in d.glob("*.md")]
    paths = sorted(paths, key=lambda p: p.name, reverse=True)
    paths = [p for p in paths if p.name == "INDEX.md"] + [p for p in paths if p.name != "INDEX.md"]
    out, seen = [], set()
    for p in paths:
        if p.name in seen:
            continue
        seen.add(p.name)
        try:
            out.append({"name": p.name, "content": p.read_text()})
        except Exception:
            pass
    return out[:40]


def _env_context(limit=4000):
    """The most recent discovery doc, so the brain actually KNOWS the environment it's in
    (connects autodiscovery to chat + thinking instead of leaving it in a folder)."""
    docs = _discovery()
    return docs[0]["content"][:limit] if docs else ""


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
    p.write_text(yaml.safe_dump(data, sort_keys=False))


def _chat_reply(conv):
    """Generate Jarvis's reply to the latest owner message in a conversation, via the brain.
    Runs in a background thread so /api/say returns instantly; the reply shows on the next poll."""
    try:
        from jarvis.config import load
        from jarvis.adapters.llm import build_llm
        from jarvis import messaging
        cfg = load()
        llm = build_llm(cfg)
        if not llm:
            return
        msgs = messaging.messages(conv, limit=20)
        transcript = "\n".join(("Owner: " if m.get("from") == "owner" else "Jarvis: ") + (m.get("text") or "")
                               for m in msgs if m.get("kind") in ("message", "note", "answer", "question"))
        ident = cfg.get("identity") or {}
        name, mode = ident.get("name", "Jarvis"), ident.get("mode", "shadow")
        from jarvis import persona
        env = _env_context()
        base = (persona.system(cfg) + " You're chatting with the owner in your dashboard."
                + (f"\n\nWhat you've discovered about your environment:\n{env}\n" if env else "")
                + f"\nConversation so far:\n{transcript}\n")
        # Auto web-use: prefer what Jarvis already knows; only browse for genuinely EXTERNAL facts.
        out = llm.run("orchestrator", base +
                      "\nAnswer the latest owner message. For ANYTHING about THIS machine, network, or "
                      "setup, answer from the environment info above — do NOT web-search it. ONLY if the "
                      "answer truly depends on EXTERNAL current-world facts you don't know (software "
                      "releases, prices, news, third-party docs) AND aren't in the environment info, "
                      f"reply with EXACTLY 'SEARCH: <query>'. Otherwise answer directly.\n\n{name}:",
                      timeout=120).strip()
        used_web = False
        if out.upper().startswith("SEARCH:"):
            query = out.split(":", 1)[1].strip()
            try:
                ans, results = _web_skill().research(query, llm)   # spawns a cheap-tier research agent
                src = "\n".join("- " + r["url"] for r in results[:3])
                out, used_web = ans + (f"\n\n(sources:\n{src})" if src else ""), True
            except Exception:
                out = llm.run("orchestrator", base + f"\n{name}:", timeout=120).strip()
        if out:
            messaging.reply(out, conv=conv)
            try:
                from jarvis import feedback
                feedback.record(cfg, kind=("chat+web" if used_web else "chat"), area=conv,
                                summary=(msgs[-1].get("text", "")[:120] if msgs else ""), outcome="replied")
            except Exception:
                pass
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
    p.write_text(yaml.safe_dump(data, sort_keys=False))


_MAIN_TARGET = {"claude": "claude:opus", "codex": "codex:gpt-5.5", "ollama": "ollama_cloud:deepseek-v4-pro"}


def _models():
    """Model cards for the MODELS tab: which brains are connected + which is the main brain."""
    cfg = {}
    try:
        from jarvis.config import load
        cfg = load()
    except Exception:
        pass
    main = ((cfg.get("llm", {}) or {}).get("routing", {}) or {}).get("orchestrator", "")
    home = Path(os.path.expanduser("~"))
    claude_conn = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
                       or (home / ".claude" / ".credentials.json").exists())
    return [
        {"id": "claude", "name": "Claude (Anthropic)", "tier": "frontier", "field": "token",
         "connected": claude_conn, "is_main": main.startswith("claude"),
         "how": "Run `claude setup-token` where you're logged into Claude, then paste the token."},
        {"id": "codex", "name": "Codex (OpenAI / ChatGPT)", "tier": "frontier", "field": "token",
         "connected": (home / ".codex" / "auth.json").exists(), "is_main": main.startswith("codex"),
         "how": "Run `codex login` on this machine, OR paste an access token (uses codex login --with-access-token)."},
        {"id": "ollama", "name": "Ollama Cloud (DeepSeek)", "tier": "agent / cheap", "field": "key",
         "connected": bool(os.environ.get("OLLAMA_API_KEY")), "is_main": main.startswith("ollama"),
         "how": "Paste your Ollama Cloud API key (subscription pricing)."},
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
        if u.path == "/api/kill":
            pid = (parse_qs(u.query).get("pid") or [""])[0]
            try:
                os.kill(int(pid), signal.SIGTERM)
                return self._send(200, json.dumps({"ok": True, "pid": pid}))
            except Exception as e:
                return self._send(400, json.dumps({"ok": False, "error": str(e)}))
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
                if prov == "claude":                       # subscription via setup-token -> MAIN brain
                    if not key:
                        return self._send(400, json.dumps({"ok": False, "error": "no token provided"}))
                    secrets.set_secret("CLAUDE_CODE_OAUTH_TOKEN", key)
                    _set_main_brain("claude:opus")
                    ok, detail = preflight.recheck_brain(load())
                    return self._send(200, json.dumps({"ok": ok, "detail": detail}))
                if prov == "codex":                        # subscription via access token -> MAIN brain
                    import shutil as _sh, subprocess as _sp
                    if not key:
                        return self._send(400, json.dumps({"ok": False, "error": "no token provided"}))
                    if not _sh.which("codex"):
                        return self._send(400, json.dumps({"ok": False, "error": "codex CLI isn't installed on this machine yet"}))
                    try:
                        _sp.run(["codex", "login", "--with-access-token"], input=key,
                                capture_output=True, text=True, timeout=30)
                    except Exception as e:
                        return self._send(500, json.dumps({"ok": False, "error": str(e)[:160]}))
                    _set_main_brain("codex:gpt-5.5")
                    ok, detail = preflight.recheck_brain(load())
                    return self._send(200, json.dumps({"ok": ok, "detail": detail}))
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
        try:
            from jarvis import messaging
            if u.path == "/api/answer":
                ok = messaging.answer(body.get("id", ""), body.get("text", ""))
                return self._send(200 if ok else 400, json.dumps({"ok": ok}))
            if u.path == "/api/say":
                conv = body.get("conv", "general")
                messaging.say(body.get("text", ""), conv=conv)
                threading.Thread(target=_chat_reply, args=(conv,), daemon=True).start()
                return self._send(200, json.dumps({"ok": True}))
            if u.path == "/api/archive":
                messaging.archive(body.get("conv", ""), bool(body.get("archived", True)))
                return self._send(200, json.dumps({"ok": True}))
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
