"""Self-training drills for a running Jarvis dashboard.

These are black-box checks against Jarvis's normal chat API. They are intentionally
model-neutral: the selected brain can be OpenAI, Claude, Ollama, DeepSeek, or anything
else wired into Jarvis. The goal is to catch harness/tool/memory regressions.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _token(path: str | None) -> str:
    p = Path(path or ROOT / "state" / "dashboard_token")
    return p.read_text(encoding="utf-8").strip()


def _request_json(url: str, data: dict | None = None):
    body = None if data is None else json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8") or "null")


def _send(base: str, token: str, conv: str, text: str) -> None:
    url = f"{base.rstrip('/')}/api/say?token={urllib.parse.quote(token)}"
    _request_json(url, {"conv": conv, "text": text})


def _messages(base: str, token: str, conv: str):
    qs = urllib.parse.urlencode({"token": token, "conv": conv})
    return _request_json(f"{base.rstrip('/')}/api/messages?{qs}")


def _ask(base: str, token: str, conv: str, text: str, timeout: int = 180) -> str:
    _send(base, token, conv, text)
    deadline = time.time() + timeout
    last = []
    while time.time() < deadline:
        time.sleep(2)
        last = _messages(base, token, conv)
        if not any(m.get("streaming") for m in last):
            break
    replies = [m for m in last if m.get("from") == "jarvis"]
    return (replies[-1].get("text") if replies else "") or ""


def _case(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail[:2000]}


def run(base: str, token: str) -> dict:
    ts = int(time.time())
    cases = []

    conv = f"selftest-direct-{ts}"
    answer = _ask(base, token, conv, "give me the uptime of this server")
    cases.append(_case(
        "direct uptime is human-formatted",
        "Uptime is" in answer and "Load average" in answer and "$ uptime" not in answer,
        answer,
    ))

    canary = f"amber-orbit-{ts}"
    conv = f"selftest-memory-{ts}"
    _ask(base, token, conv, f"do you know what the {canary} marker is?")
    correction = f"Correction: the {canary} marker is a Jarvis self-training canary for durable correction recall."
    _ask(base, token, conv, correction)
    fresh = f"selftest-memory-fresh-{ts}"
    answer = _ask(base, token, fresh, f"what is the {canary} marker?")
    cases.append(_case(
        "correction persists across fresh conversation",
        canary in answer and "durable correction recall" in answer,
        answer,
    ))

    conv = f"selftest-multiturn-{ts}"
    first = _ask(base, token, conv, "where is your local indexing implemented? answer with files and what each one does.")
    second = _ask(base, token, conv, "what evidence did you use for that?")
    cases.append(_case(
        "follow-up can cite prior turn evidence",
        "local_knowledge.py" in first and "local_knowledge.py" in second,
        f"FIRST:\n{first}\n\nSECOND:\n{second}",
    ))

    report = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": all(c["ok"] for c in cases), "cases": cases}
    _write_report(report)
    return report


def _write_report(report: dict) -> None:
    out = ROOT / "workspace" / "knowledge" / "evals"
    out.mkdir(parents=True, exist_ok=True)
    stamp = str(int(time.time()))
    (out / f"{stamp}-self-training.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["# Self-training drill report", "", f"- Time: {report['ts']}", f"- Result: {'PASS' if report['ok'] else 'FAIL'}", ""]
    for case in report["cases"]:
        lines += [f"## {'PASS' if case['ok'] else 'FAIL'}: {case['name']}", "", "```text", case["detail"], "```", ""]
    (out / "SELF_TRAINING_LAST_RUN.md").write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8787")
    ap.add_argument("--token-file", default=None)
    args = ap.parse_args()
    report = run(args.base, _token(args.token_file))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
