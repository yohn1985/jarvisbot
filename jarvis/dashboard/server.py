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
import argparse, json, os, signal, subprocess, sys
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
        if u.path == "/api/kill":
            pid = (parse_qs(u.query).get("pid") or [""])[0]
            try:
                os.kill(int(pid), signal.SIGTERM)
                return self._send(200, json.dumps({"ok": True, "pid": pid}))
            except Exception as e:
                return self._send(400, json.dumps({"ok": False, "error": str(e)}))
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
