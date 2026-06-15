"""Self-training drills for a running Jarvis dashboard.

These are black-box checks against Jarvis's normal chat API. They are intentionally
model-neutral: the selected brain can be OpenAI, Claude, Ollama, DeepSeek, or anything
else wired into Jarvis. The goal is to catch harness/tool/memory regressions.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
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
    trace = thinking + "\n" + (msg.get("evidence") or "")
    cases.append(_case(
        "real Python code edit emits visible edit marker",
        "age_hours > 48" in current
        and ("25" in current or "0.25" in current)
        and priority_after_48 >= priority_before_48 + 20.0
        and "JARVIS_EDIT" in trace
        and "calculate_priority" in text,
        (
            f"ANSWER:\n{text}\n\n"
            f"TRACE_HAS_MARKER:{'JARVIS_EDIT' in trace}\n"
            f"PRIORITY_47H:{priority_before_48}\n"
            f"PRIORITY_49H:{priority_after_48}\n\n"
            f"SNIP:\n{current[current.find('def calculate_priority'):current.find('def calculate_priority') + 900]}"
        ),
    ))

    create_path = Path(f"/tmp/jarvis-selftest-created-module-{ts}.py")
    if create_path.exists():
        create_path.unlink()
    conv = f"selftest-natural-create-{ts}"
    msg = _ask_message(
        base,
        token,
        conv,
        (
            f"Create a new Python module at {create_path} for incident routing. "
            "Use normal coding-agent behavior, not an exact replacement drill. "
            "The file must be at least 120 lines and include a Task dataclass plus "
            "functions named normalize_severity, route_incident, and summarize_queue. "
            "After writing it, tell me the file path and one behavior it implements."
        ),
        timeout=240,
    )
    created = create_path.read_text(encoding="utf-8", errors="replace") if create_path.exists() else ""
    created_lines = created.splitlines()
    create_text = msg.get("text") or ""
    cases.append(_case(
        "natural-language create-file task writes a real 120+ line module",
        create_path.exists()
        and len(created_lines) >= 120
        and "dataclass" in created
        and "def normalize_severity" in created
        and "def route_incident" in created
        and "def summarize_queue" in created
        and str(create_path) in create_text,
        (
            f"ANSWER:\n{create_text}\n\n"
            f"EXISTS:{create_path.exists()}\n"
            f"LINES:{len(created_lines)}\n"
            f"SNIP:\n{created[:1200]}"
        ),
    ))

    route_path = Path(f"/tmp/jarvis-selftest-route-module-{ts}.py")
    route_path.write_text(_sample_incident_module(), encoding="utf-8")
    conv = f"selftest-natural-edit-{ts}"
    msg = _ask_message(
        base,
        token,
        conv,
        (
            f"Modify the existing Python file {route_path}. Change only the route_incident "
            "function so billing incidents with high or critical severity return "
            "finance-oncall before the normal product routing. Do not ask me for exact old "
            "and new text. After editing, tell me what changed."
        ),
        timeout=240,
    )
    routed_low, routed_high, routed_critical = _load_route_values(route_path)
    edited = route_path.read_text(encoding="utf-8", errors="replace")
    edit_text = msg.get("text") or ""
    edit_thinking = msg.get("thinking") or ""
    edit_trace = edit_thinking + "\n" + (msg.get("evidence") or "")
    cases.append(_case(
        "natural-language named-function edit changes behavior and emits edit marker",
        routed_low == "product-oncall"
        and routed_high == "finance-oncall"
        and routed_critical == "finance-oncall"
        and "JARVIS_EDIT" in edit_trace
        and "route_incident" in edit_text,
        (
            f"ANSWER:\n{edit_text}\n\n"
            f"TRACE_HAS_MARKER:{'JARVIS_EDIT' in edit_trace}\n"
            f"BILLING_LOW:{routed_low}\n"
            f"BILLING_HIGH:{routed_high}\n"
            f"BILLING_CRITICAL:{routed_critical}\n\n"
            f"SNIP:\n{edited[edited.find('def route_incident'):edited.find('def route_incident') + 900]}"
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
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return (
        float(module.calculate_priority(10.0, 47.0)),
        float(module.calculate_priority(10.0, 49.0)),
    )


def _sample_incident_module() -> str:
    lines = [
        '"""Sample incident routing module for natural-language edit drills."""',
        "",
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass",
        "",
        "@dataclass",
        "class Incident:",
        "    kind: str",
        "    severity: str",
        "    title: str = ''",
        "",
        "SEVERITY_ORDER = {",
        "    'low': 1,",
        "    'medium': 2,",
        "    'high': 3,",
        "    'critical': 4,",
        "}",
        "",
        "def normalize_severity(value: str) -> str:",
        "    cleaned = (value or '').strip().lower()",
        "    if cleaned in {'sev1', 'p0', 'urgent'}:",
        "        return 'critical'",
        "    if cleaned in {'sev2', 'p1'}:",
        "        return 'high'",
        "    if cleaned in {'sev3', 'p2'}:",
        "        return 'medium'",
        "    if cleaned in SEVERITY_ORDER:",
        "        return cleaned",
        "    return 'low'",
        "",
        "def route_incident(kind: str, severity: str) -> str:",
        "    normalized_kind = (kind or '').strip().lower()",
        "    normalized_severity = normalize_severity(severity)",
        "    if normalized_kind in {'security', 'abuse'}:",
        "        return 'security-oncall'",
        "    if normalized_severity == 'critical':",
        "        return 'incident-commander'",
        "    if normalized_kind in {'billing', 'payments', 'subscription'}:",
        "        return 'product-oncall'",
        "    if normalized_kind in {'infrastructure', 'database', 'network'}:",
        "        return 'platform-oncall'",
        "    return 'support-triage'",
        "",
        "def summarize_queue(incidents: list[Incident]) -> dict[str, int]:",
        "    summary: dict[str, int] = {}",
        "    for incident in incidents:",
        "        route = route_incident(incident.kind, incident.severity)",
        "        summary[route] = summary.get(route, 0) + 1",
        "    return summary",
        "",
    ]
    for idx in range(1, 95):
        lines.extend([
            f"def policy_note_{idx}() -> str:",
            f"    return 'policy-{idx}'",
            "",
        ])
    return "\n".join(lines)


def _load_route_values(path: Path) -> tuple[str, str, str]:
    spec = importlib.util.spec_from_file_location(f"jarvis_route_{path.stem}", path)
    if not spec or not spec.loader:
        return "", "", ""
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return (
        str(module.route_incident("billing", "low")),
        str(module.route_incident("billing", "high")),
        str(module.route_incident("billing", "critical")),
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
