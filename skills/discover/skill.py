#!/usr/bin/env python3
"""discover — Jarvis's autodiscovery (the whole point).

Land on a machine, look around, and have the brain write down what it learned:
    SCAN (pure Python/subprocess) -> ORGANIZE (LLM) -> PERSIST (workspace/discovery/<host>.md)

Scanning is bounded + degrade-safe (a missing tool is skipped). Organizing runs on Jarvis's
configured brain. Network-device discovery + persisting into the knowledge tables come next.

    python skill.py [--print]
"""
from __future__ import annotations
import argparse, platform, socket, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from jarvis.config import load
from jarvis.adapters.llm import build_llm


def _run(cmd, cap=4000):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout[:cap]
    except Exception as e:
        return f"(unavailable: {str(e)[:60]})"


def scan_host() -> dict:
    """Bounded, degrade-safe facts about this machine and what it can see."""
    facts = {"hostname": socket.gethostname(), "kernel": platform.platform()}
    try:
        facts["os"] = Path("/etc/os-release").read_text()[:500]
    except Exception:
        facts["os"] = ""
    facts["uptime_load"] = _run(["uptime"], 200)
    facts["cpu_mem"] = _run(["sh", "-c", "echo cpus=$(nproc); free -h"], 400)
    facts["disks"] = _run(["df", "-h", "-x", "tmpfs", "-x", "devtmpfs"], 1500)
    facts["services_running"] = _run(["systemctl", "list-units", "--type=service",
                                      "--state=running", "--no-legend", "--no-pager"], 4000)
    facts["listening_ports"] = _run(["sh", "-c", "ss -ltnp 2>/dev/null || ss -ltn"], 3000)
    facts["net_interfaces"] = _run(["sh", "-c", "ip -br addr 2>/dev/null"], 1000)
    facts["routes"] = _run(["sh", "-c", "ip route 2>/dev/null"], 1000)
    facts["neighbours"] = _run(["sh", "-c", "ip neigh 2>/dev/null"], 2000)
    facts["containers"] = _run(["sh", "-c", "docker ps --format '{{.Names}}: {{.Image}}' 2>/dev/null"], 1500)
    return facts


def organize(llm, facts: dict) -> str:
    blob = "\n\n".join(f"## {k}\n{v}" for k, v in facts.items() if v and not v.startswith("(unavailable"))
    prompt = (
        "You just landed on a Linux machine and must document it for your own future memory. "
        "From these RAW FACTS, write a clear, factual markdown document covering: what this machine "
        "is and its likely role, the services it runs (with ports), networking (interfaces, routes, "
        "and neighbours/other devices seen on the network), storage, containers, and anything "
        "notable. Finish with a short 'Open questions / worth investigating' list. Be concise and "
        "do NOT invent anything that isn't in the facts.\n\n"
        f"RAW FACTS:\n{blob[:14000]}"
    )
    return llm.run("orchestrator", prompt, timeout=240).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", help="also print the document")
    a = ap.parse_args()
    cfg = load()
    llm = build_llm(cfg)
    if not llm:
        sys.exit("discover: no brain configured (set up an AI brain first)")

    print("[discover] scanning host...", file=sys.stderr)
    facts = scan_host()
    print("[discover] organizing with the brain...", file=sys.stderr)
    doc = organize(llm, facts)

    out_dir = ROOT / "workspace" / "discovery"
    out_dir.mkdir(parents=True, exist_ok=True)
    host = facts.get("hostname", "host")
    path = out_dir / f"{host}-{time.strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(f"# Discovery: {host}\n_generated {time.strftime('%Y-%m-%d %H:%M:%S')}_\n\n{doc}\n")
    print(f"[discover] wrote {path}")
    if a.print:
        print("\n" + doc)


if __name__ == "__main__":
    main()
