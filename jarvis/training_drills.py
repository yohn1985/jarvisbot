"""Self-training drills for a running Jarvis dashboard.

These are black-box checks against Jarvis's normal chat API. They are intentionally
model-neutral: the selected brain can be OpenAI, Claude, Ollama, DeepSeek, or anything
else wired into Jarvis. The goal is to catch harness/tool/memory regressions.
"""
from __future__ import annotations

import argparse
import importlib.util
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
    msg = _ask_message(base, token, conv, text, timeout=timeout)
    return (msg.get("text") if msg else "") or ""


def _ask_message(base: str, token: str, conv: str, text: str, timeout: int = 180) -> dict:
    _send(base, token, conv, text)
    deadline = time.time() + timeout
    last = []
    while time.time() < deadline:
        time.sleep(2)
        last = _messages(base, token, conv)
        if not any(m.get("streaming") for m in last):
            break
    replies = [m for m in last if m.get("from") == "jarvis"]
    return replies[-1] if replies else {}


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
    low_answer = answer.lower()
    cases.append(_case(
        "correction persists across fresh conversation",
        canary in answer and "durable correction recall" in low_answer
        and "no evidence" not in low_answer and "unresolved" not in low_answer and "similar marker" not in low_answer,
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

    path = Path(f"/tmp/jarvis-selftest-edit-{ts}.txt")
    path.write_text("before-value\n", encoding="utf-8")
    conv = f"selftest-edit-{ts}"
    answer = _ask(base, token, conv, f"Edit {path} replacing before-value with after-value, then tell me whether it worked.")
    current = path.read_text(encoding="utf-8", errors="replace")
    cases.append(_case(
        "safe edit tool reports successful mutation",
        current.strip() == "after-value" and "after-value" in answer and "failed" not in answer.lower(),
        f"ANSWER:\n{answer}\n\nFILE:\n{current}",
    ))

    code_path = Path(f"/tmp/jarvis-selftest-real-code-{ts}.py")
    code_path.write_text(_sample_python_module(), encoding="utf-8")
    conv = f"selftest-real-code-edit-{ts}"
    msg = _ask_message(
        base,
        token,
        conv,
        (
            f"Edit the real Python code in {code_path}. Change calculate_priority so tasks older "
            "than 48 hours get 25 extra urgency points, keep the rest of the module intact, "
            "then tell me what changed."
        ),
        timeout=240,
    )
    current = code_path.read_text(encoding="utf-8", errors="replace")
    priority_before_48, priority_after_48 = _load_priority_values(code_path)
    text = (msg.get("text") or "")
    thinking = (msg.get("thinking") or "")
    cases.append(_case(
        "real Python code edit emits visible edit marker",
        "age_hours > 48" in current
        and ("25" in current or "0.25" in current)
        and priority_after_48 >= priority_before_48 + 20.0
        and "JARVIS_EDIT" in thinking
        and "calculate_priority" in text,
        (
            f"ANSWER:\n{text}\n\n"
            f"THINKING_HAS_MARKER:{'JARVIS_EDIT' in thinking}\n"
            f"PRIORITY_47H:{priority_before_48}\n"
            f"PRIORITY_49H:{priority_after_48}\n\n"
            f"SNIP:\n{current[current.find('def calculate_priority'):current.find('def calculate_priority') + 900]}"
        ),
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


def _sample_python_module() -> str:
    lines = [
        '"""Sample module used by Jarvis self-training real-code edit drills."""',
        "",
        "from __future__ import annotations",
        "",
        "MAX_PRIORITY = 100.0",
        "DEFAULT_SCORE_WEIGHT = 0.7",
        "DEFAULT_AGE_WEIGHT = 0.3",
        "",
        "class TaskRecord:",
        "    def __init__(self, task_id: str, score: float, age_hours: float) -> None:",
        "        self.task_id = task_id",
        "        self.score = score",
        "        self.age_hours = age_hours",
        "",
        "def clamp(value: float, low: float, high: float) -> float:",
        "    return max(low, min(value, high))",
        "",
        "def calculate_priority(score: float, age_hours: float) -> float:",
        "    norm_score = clamp(score, 0.0, 100.0) / 100.0",
        "    age_factor = clamp(age_hours / 168.0, 0.0, 1.0)",
        "    urgency = (DEFAULT_SCORE_WEIGHT * norm_score) + (DEFAULT_AGE_WEIGHT * age_factor)",
        "    return MAX_PRIORITY * clamp(urgency, 0.0, 1.0)",
        "",
        "def rank_tasks(tasks: list[TaskRecord]) -> list[TaskRecord]:",
        "    return sorted(tasks, key=lambda task: calculate_priority(task.score, task.age_hours))",
        "",
    ]
    for idx in range(1, 100):
        lines.extend([
            f"def helper_{idx}(value: float) -> float:",
            f"    return value + {idx}.0",
            "",
        ])
    return "\n".join(lines)


def _load_priority_values(path: Path) -> tuple[float, float]:
    spec = importlib.util.spec_from_file_location(f"jarvis_selftest_{path.stem}", path)
    if not spec or not spec.loader:
        return 0.0, 0.0
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return (
        float(module.calculate_priority(10.0, 47.0)),
        float(module.calculate_priority(10.0, 49.0)),
    )


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
