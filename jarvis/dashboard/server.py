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
        env = _env_context()
        prompt = (f"You are {name}, the owner's personal autonomous ops/dev agent, chatting in your "
                  f"dashboard (mode: {mode}). Reply concisely and directly to the latest owner message."
                  + (f"\n\nWhat you've discovered about your environment (use it when relevant):\n{env}\n"
                     if env else "")
                  + f"\nConversation so far:\n{transcript}\n\n{name}:")
        out = llm.run("orchestrator", prompt, timeout=120).strip()
        if out:
            messaging.reply(out, conv=conv)
    except Exception:
        pass


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
                env_name = {"ollama": "OLLAMA_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                            "openai": "OPENAI_API_KEY"}.get(prov, prov.upper() + "_API_KEY")
                if key:
                    secrets.set_secret(env_name, key)
                if prov == "ollama":
                    secrets.load_env()
                    if not os.environ.get("OLLAMA_API_KEY"):
                        return self._send(400, json.dumps({"ok": False, "error": "no Ollama key stored yet"}))
                    _enable_ollama_cloud()                 # wire backend + routing to the stored key
                    from jarvis.config import load
                    from jarvis.bootstrap import preflight
                    ok, detail = preflight.recheck_brain(load())   # probe now -> brain live
                    return self._send(200, json.dumps({"ok": ok, "detail": detail}))
                if not key:
                    return self._send(400, json.dumps({"ok": False, "error": "no key provided"}))
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
