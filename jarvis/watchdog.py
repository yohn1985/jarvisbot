"""Pure-Python self-monitoring + self-healing (NO AI).

The loop calls watch() every wake BEFORE the AI tick, so the core keeps itself alive even when the
brain is down or missing:
  - dashboard not responding on :8787  -> restart it (systemctl if managed, else relaunch).
  - brain unavailable                  -> record it + post a note once (the loop keeps running
    degraded; the LLM router's fallbacks are the AI backup).

Health is written to state/health.json. Stdlib only — this is the layer that must work with no AI.
"""
from __future__ import annotations
import json, os, subprocess, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("JARVIS_DASHBOARD_PORT", "8787"))
HEALTH = ROOT / "state" / "health.json"


def _responding(url, timeout=4):
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True            # 401/403 still means the server is up
    except Exception:
        return False


def _note(text):
    try:
        from jarvis import messaging
        messaging.post_note(text, conv="activity", title="Activity")
    except Exception:
        pass


def _managed(unit):
    try:
        return subprocess.run(["systemctl", "is-enabled", unit], capture_output=True,
                              text=True, timeout=10).returncode == 0
    except Exception:
        return False


def check_dashboard():
    url = f"http://127.0.0.1:{PORT}/"
    if _responding(url):
        return "ok"
    if _managed("jarvis-dashboard"):
        subprocess.run(["sudo", "-n", "systemctl", "restart", "jarvis-dashboard"],
                       capture_output=True, text=True, timeout=40)
    else:                       # not under systemd: relaunch it ourselves
        py = ROOT / ".venv" / "bin" / "python"
        py = str(py) if py.exists() else "python3"
        try:
            (ROOT / "state").mkdir(exist_ok=True)
            with open(ROOT / "state" / "dashboard.log", "ab") as logf:
                subprocess.Popen([py, str(ROOT / "jarvis" / "dashboard" / "server.py"),
                                  "--host", "0.0.0.0", "--port", str(PORT)],
                                 stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                                 start_new_session=True)
        except Exception:
            pass
    time.sleep(2)
    healed = _responding(url)
    _note("Dashboard wasn't responding — " + ("restarted it, back up." if healed else
                                              "tried to restart it (still checking)."))
    return "healed" if healed else "down"


def check_brain(cfg):
    """Cheap availability check (NO LLM call): is any configured brain usable right now?"""
    try:
        from jarvis.bootstrap import preflight
        if preflight.find_ai_clis(cfg):
            return "ok"
        for spec in (cfg.get("llm", {}).get("backends") or {}).values():
            if isinstance(spec, dict) and spec.get("http"):
                env = spec.get("api_key_env")
                if not env or os.environ.get(env):
                    return "ok"
        return "missing"
    except Exception:
        return "unknown"


def watch(cfg):
    prev = {}
    try:
        prev = json.loads(HEALTH.read_text())
    except Exception:
        pass
    health = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "dashboard": check_dashboard(), "brain": check_brain(cfg)}
    try:
        (ROOT / "state").mkdir(exist_ok=True)
        HEALTH.write_text(json.dumps(health, indent=2))
    except Exception:
        pass
    if health["brain"] == "missing" and prev.get("brain") != "missing":   # note only on transition
        _note("My AI brain looks unavailable — I'll keep monitoring and running on what I can until "
              "it's back. Add or fix a brain in the dashboard if needed.")
    return health
