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
import argparse, json, os, signal, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
HTML = Path(__file__).resolve().parent / "dashboard.html"


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


# --- bootstrap setup (the dashboard's guided checklist) ---
_EXEC = {"running": False, "last": None}
_EXEC_LOCK = threading.Lock()


def _setup_state():
    """One simple snapshot the UI uses to show the next thing for the owner to do."""
    try:
        from jarvis.config import load
        from jarvis.bootstrap import preflight, installer
        cfg = load()
        st = preflight.status(cfg)
        plan = installer.plan(cfg)
        return {"ready": st["ready"], "checks": st["checks"], "plan": plan,
                "running": _EXEC["running"], "last": _EXEC["last"],
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
    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, HTML.read_text() if HTML.exists() else "<h1>Jarvis</h1>", "text/html")
        if u.path == "/api/runs":
            return self._send(200, json.dumps(_runs()))
        if u.path == "/api/config":
            return self._send(200, json.dumps(_config()))
        if u.path == "/api/procs":
            return self._send(200, json.dumps(_procs()))
        if u.path == "/api/setup":
            return self._send(200, json.dumps(_setup_state()))
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
        try:
            from jarvis import messaging
            if u.path == "/api/answer":
                ok = messaging.answer(body.get("id", ""), body.get("text", ""))
                return self._send(200 if ok else 400, json.dumps({"ok": ok}))
            if u.path == "/api/say":
                messaging.say(body.get("text", ""), conv=body.get("conv", "general"))
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
