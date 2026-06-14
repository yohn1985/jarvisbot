#!/usr/bin/env python3
"""discover — Jarvis's autodiscovery (the whole point).

Land on a machine, look around, and have the brain write down what it learned:
    SCAN (pure Python/subprocess) -> ORGANIZE (LLM) -> PERSIST (workspace/discovery/<host>.md)

Scanning is bounded + degrade-safe (a missing tool is skipped). Organizing runs on Jarvis's
configured brain. Network-device discovery + persisting into the knowledge tables come next.

    python skill.py [--print]
"""
from __future__ import annotations
import argparse, concurrent.futures, ipaddress, json, platform, re, socket, subprocess, sys, time, uuid
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


DISC_DIR = ROOT / "workspace" / "discovery"
KNOW_DIR = ROOT / "workspace" / "knowledge"
Q_FILE = KNOW_DIR / "questions.json"


def _load_q():
    try:
        return json.loads(Q_FILE.read_text())
    except Exception:
        return []


def _save_q(q):
    KNOW_DIR.mkdir(parents=True, exist_ok=True)
    Q_FILE.write_text(json.dumps(q, indent=2))


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:50] or "q"


def extract_questions(doc: str):
    """Pull the 'open questions / worth investigating' list items out of a discovery doc."""
    qs, grab = [], False
    for line in doc.splitlines():
        if line.lstrip().startswith("#"):
            h = line.lower()
            grab = ("open question" in h or "worth investigating" in h or "investigate" in h)
            continue
        if grab:
            m = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*)", line)
            if m:
                q = re.sub(r"\*\*|`", "", m.group(1)).strip()
                if len(q) > 8:
                    qs.append(q)
    return qs


def add_questions(qs):
    queue = _load_q()
    have = {(x.get("q") or "").lower() for x in queue}
    for q in qs:
        if q.lower() not in have:
            queue.append({"id": uuid.uuid4().hex[:8], "q": q, "answered": False,
                          "answer": None, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
            have.add(q.lower())
    _save_q(queue)


def _known_context(limit=9000):
    docs = sorted((p for p in DISC_DIR.glob("*.md") if p.name != "INDEX.md"), reverse=True) if DISC_DIR.exists() else []
    return "".join(f"\n\n=== {p.name} ===\n{p.read_text()}" for p in docs[:3])[:limit]


def answer_one(llm):
    """Take the oldest open question and answer it from accumulated knowledge — Jarvis building
    understanding one question at a time. Returns (question, answer) or None."""
    queue = _load_q()
    nxt = next((x for x in queue if not x.get("answered")), None)
    if not nxt:
        return None
    prompt = ("You are building your own understanding of your environment. Using your existing notes "
              "below, investigate and answer this question as specifically as you can. If you truly "
              "cannot answer from what's known, state exactly what data you'd need to find out.\n\n"
              f"QUESTION: {nxt['q']}\n\nYOUR NOTES:\n{_known_context()}")
    ans = llm.run("orchestrator", prompt, timeout=180).strip()
    # deep-fix discipline: red-team the answer; revise once if it doesn't survive.
    try:
        from jarvis.config import load as _load
        from jarvis import verify as _verify
        ctx = _known_context()
        v = _verify.verify(_load(), ans, context=ctx)
        if not v["survives"]:
            ans = llm.run("orchestrator",
                          f"Your draft answer was challenged by a red-team check. Critique:\n{v['critique']}\n\n"
                          f"QUESTION: {nxt['q']}\nRevise to honestly address it; if still unsure, say what's "
                          f"unverified.\n\nNOTES:\n{ctx}", timeout=180).strip()
    except Exception:
        pass
    KNOW_DIR.mkdir(parents=True, exist_ok=True)
    note = KNOW_DIR / f"{_slug(nxt['q'])}.md"     # logical name per question, updated in place
    note.write_text(f"# Q: {nxt['q']}\n_answered {time.strftime('%Y-%m-%d %H:%M:%S')}_\n\n{ans}\n")
    nxt["answered"], nxt["answer"] = True, ans[:400]
    _save_q(queue)
    write_index()
    return nxt["q"], ans


def write_index():
    """INDEX.md: open questions Jarvis is exploring + discovery docs + answered notes."""
    queue = _load_q()
    open_qs = [x for x in queue if not x.get("answered")]
    docs = sorted((p for p in DISC_DIR.glob("*.md") if p.name != "INDEX.md"), reverse=True) if DISC_DIR.exists() else []
    notes = sorted(KNOW_DIR.glob("*.md"), reverse=True) if KNOW_DIR.exists() else []
    lines = ["# Jarvis Knowledge — Index", f"_updated {time.strftime('%Y-%m-%d %H:%M:%S')}_", ""]
    if open_qs:
        lines.append("## Open questions I'm still exploring")
        lines += [f"- {x['q']}" for x in open_qs[:15]] + [""]
    lines.append("## Environment discovery")
    lines += ([f"- [{p.stem}]({p.name})" for p in docs] or ["_none yet_"])
    if notes:
        lines += ["", "## What I've learned (answered)"]
        lines += [f"- [{p.stem}]({p.name})" for p in notes[:25]]
    DISC_DIR.mkdir(parents=True, exist_ok=True)
    (DISC_DIR / "INDEX.md").write_text("\n".join(lines) + "\n")


SUGGEST_MARKER = ROOT / "state" / "suggest.json"


def _knowledge_sig():
    docs = len(list(DISC_DIR.glob("*.md"))) if DISC_DIR.exists() else 0
    notes = len(list(KNOW_DIR.glob("*.md"))) if KNOW_DIR.exists() else 0
    return f"{docs}:{notes}"


def suggest(llm, force=False):
    """Review accumulated knowledge and proactively propose work to the owner — the 'super employee'
    move. Gated on the knowledge signature so it only proposes when it has actually learned more."""
    sig = _knowledge_sig()
    try:
        last = json.loads(SUGGEST_MARKER.read_text()).get("sig")
    except Exception:
        last = None
    if not force and sig == last:
        return None
    notes = sorted(KNOW_DIR.glob("*.md"), reverse=True) if KNOW_DIR.exists() else []
    learned = "".join(f"\n\n=== {p.name} ===\n{p.read_text()}" for p in notes[:5])
    prompt = ("From your accumulated knowledge of this environment, propose 3-5 concrete, prioritized, "
              "actionable things the owner might want done — improvements, risks, fixes, optimizations, "
              "or worthwhile follow-ups. For each: a one-line **title** then a short 'why'. Be specific "
              "to what you actually found; do not invent.\n\n"
              f"DISCOVERY:\n{_known_context()}\n\nWHAT YOU'VE LEARNED:\n{learned}")
    out = llm.run("orchestrator", prompt, timeout=200).strip()
    SUGGEST_MARKER.parent.mkdir(parents=True, exist_ok=True)
    SUGGEST_MARKER.write_text(json.dumps({"sig": sig, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}))
    try:
        from jarvis import messaging
        messaging.post_note("Here are things I think are worth doing:\n\n" + out,
                            conv="suggestions", title="Suggestions")
    except Exception:
        pass
    return out


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
    ap.add_argument("--answer-one", action="store_true", help="answer one open question from the queue")
    ap.add_argument("--suggest", action="store_true", help="propose work to the owner from what's known")
    a = ap.parse_args()
    cfg = load()
    llm = build_llm(cfg)
    if not llm:
        sys.exit("discover: no brain configured (set up an AI brain first)")

    if a.answer_one:                       # learning mode: answer one open question, keep building
        res = answer_one(llm)
        print(f"[discover] answered: {res[0]}" if res else "[discover] no open questions")
        return
    if a.suggest:                          # propose work to the owner from accumulated knowledge
        out = suggest(llm, force=True)
        print("[discover] suggested:\n" + out if out else "[discover] nothing new to suggest")
        return

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

    DISC_DIR.mkdir(parents=True, exist_ok=True)
    host = facts.get("hostname", "host")
    path = DISC_DIR / f"{_slug(host)}.md"        # ONE stable, logically-named doc per host — updated in place
    path.write_text(f"# Discovery: {host}\n_updated {time.strftime('%Y-%m-%d %H:%M:%S')}_\n\n{doc}\n")
    add_questions(extract_questions(doc))   # queue the doc's open questions for future cycles
    write_index()
    print(f"[discover] updated {path} (+ INDEX.md, queued questions)")
    if a.print:
        print("\n" + doc)


if __name__ == "__main__":
    main()
