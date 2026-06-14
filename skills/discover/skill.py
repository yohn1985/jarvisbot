#!/usr/bin/env python3
"""discover — Jarvis's autodiscovery (the whole point).

Land on a machine, look around, and have the brain write down what it learned:
    SCAN (pure Python/subprocess) -> ORGANIZE (LLM) -> PERSIST (workspace/discovery/<host>.md)

Scanning is bounded + degrade-safe (a missing tool is skipped). Organizing runs on Jarvis's
configured brain. Network-device discovery + persisting into the knowledge tables come next.

    python skill.py [--print]
"""
from __future__ import annotations
import argparse, concurrent.futures, ipaddress, platform, socket, subprocess, sys, time
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


_COMMON_PORTS = [22, 53, 80, 135, 139, 443, 445, 2049, 3000, 3001, 3306, 5000,
                 5432, 6379, 8000, 8080, 8443, 8787, 9000, 9090, 11434]


def _is_ip(s):
    try:
        ipaddress.ip_address(s)
        return True
    except Exception:
        return False


def _neighbours():
    out = _run(["sh", "-c", "ip neigh 2>/dev/null"], 4000)
    return {p[0] for p in (l.split() for l in out.splitlines()) if p and _is_ip(p[0])}


def _local_subnets():
    out = _run(["sh", "-c", "ip -o -4 addr show scope global 2>/dev/null | awk '{print $4}'"], 500)
    nets = []
    for cidr in out.split():
        try:
            nets.append(ipaddress.ip_interface(cidr).network)
        except Exception:
            pass
    return nets


def _port_open(host, port, timeout=0.3):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        r = s.connect_ex((host, port))
        s.close()
        return r == 0
    except Exception:
        return False


def scan_network(max_hosts=256):
    """Bounded, threaded sweep: ARP neighbours first, then fill from local /24-ish subnets. Returns
    {host: [open ports]}. Noisy-ish, so it's opt-in (--network / consent-gated in the loop)."""
    targets = list(_neighbours())
    for net in _local_subnets():
        if net.num_addresses <= 1024:
            for ip in net.hosts():
                s = str(ip)
                if s not in targets:
                    targets.append(s)
                if len(targets) >= max_hosts:
                    break
        if len(targets) >= max_hosts:
            break
    targets = targets[:max_hosts]

    def _scan(h):
        return h, [p for p in _COMMON_PORTS if _port_open(h, p)]

    found = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=120) as ex:
        for h, ports in ex.map(_scan, targets):
            if ports:
                found[h] = ports
    return found


def write_index(out_dir: Path):
    """Regenerate INDEX.md linking to every discovery doc — Jarvis's own table of contents."""
    docs = sorted((p for p in out_dir.glob("*.md") if p.name != "INDEX.md"), reverse=True)
    lines = ["# Jarvis Knowledge — Discovery Index", f"_updated {time.strftime('%Y-%m-%d %H:%M:%S')}_", ""]
    lines += [f"- [{p.stem}]({p.name})" for p in docs] or ["_(nothing discovered yet)_"]
    (out_dir / "INDEX.md").write_text("\n".join(lines) + "\n")


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
    ap.add_argument("--network", action="store_true", help="also sweep the local subnet for devices/ports")
    a = ap.parse_args()
    cfg = load()
    llm = build_llm(cfg)
    if not llm:
        sys.exit("discover: no brain configured (set up an AI brain first)")

    print("[discover] scanning host...", file=sys.stderr)
    facts = scan_host()
    if a.network:
        print("[discover] scanning local network (bounded)...", file=sys.stderr)
        net = scan_network()
        facts["network_scan"] = "\n".join(
            f"{h}: ports {', '.join(map(str, ports))}"
            for h, ports in sorted(net.items(), key=lambda kv: ipaddress.ip_address(kv[0]))
        ) or "(no responsive hosts/ports found)"
    print("[discover] organizing with the brain...", file=sys.stderr)
    doc = organize(llm, facts)

    out_dir = ROOT / "workspace" / "discovery"
    out_dir.mkdir(parents=True, exist_ok=True)
    host = facts.get("hostname", "host")
    path = out_dir / f"{host}-{time.strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(f"# Discovery: {host}\n_generated {time.strftime('%Y-%m-%d %H:%M:%S')}_\n\n{doc}\n")
    write_index(out_dir)
    print(f"[discover] wrote {path} (+ INDEX.md)")
    if a.print:
        print("\n" + doc)


if __name__ == "__main__":
    main()
