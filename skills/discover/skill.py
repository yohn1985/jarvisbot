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

    found = {}; done = 0; total = len(targets)
    print(f"[discover]   sweeping {total} hosts × {len(_COMMON_PORTS)} ports (closed ports wait on a 0.3s timeout)…", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=120) as ex:
        for h, ports in ex.map(_scan, targets):
            done += 1
            if ports:
                found[h] = ports
            if done % 25 == 0 or done == total:
                print(f"[discover]   swept {done}/{total} hosts · {len(found)} live so far", flush=True)
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


_STOPWORDS = {"the", "a", "an", "is", "are", "of", "to", "in", "on", "for", "what", "which", "how",
              "does", "do", "this", "that", "and", "or", "its", "it", "be", "with", "by", "any"}


def _qnorm(q: str) -> frozenset:
    """Normalized fingerprint of a question (significant word stems) so near-duplicates like
    'what role does host .50 play' and 'what is the role of host .50' collapse to one."""
    toks = re.findall(r"[a-z0-9.]+", (q or "").lower())
    return frozenset(t for t in toks if t not in _STOPWORDS and len(t) > 1)


def add_questions(qs):
    queue = _load_q()
    have = {_qnorm(x.get("q") or "") for x in queue}
    for q in qs:
        q = (q or "").strip()
        fp = _qnorm(q)
        if q and fp and fp not in have:      # dedup by meaning, not exact string
            queue.append({"id": uuid.uuid4().hex[:8], "q": q, "answered": False,
                          "answer": None, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
            have.add(fp)
    _save_q(queue)


def _known_context(limit=9000):
    docs = sorted((p for p in DISC_DIR.glob("*.md") if p.name != "INDEX.md"), reverse=True) if DISC_DIR.exists() else []
    return "".join(f"\n\n=== {p.name} ===\n{p.read_text()}" for p in docs[:3])[:limit]


_EXTERNAL_HINTS = ("version", "release", "released", "latest", "newest", "current", "best practice",
                   "recommended", "cve", "vulnerab", "upstream", "docs", "documentation", "how to",
                   "standard", "deprecat", "eol", "support", "compatible", "pricing", "cost",
                   "2024", "2025", "2026", "is there", "does ", "should i", "vs ")


def _looks_external(q: str) -> bool:
    """A question whose answer depends on the outside world (not just this machine) — confirm it
    on the live web instead of trusting the model's training (skeptical / deep-fix discipline)."""
    ql = q.lower()
    return any(h in ql for h in _EXTERNAL_HINTS)


def _web_evidence(q: str, llm) -> str:
    """Pull live web evidence for an external question via the shipped web skill. Best-effort."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("jarvis_web_skill", str(ROOT / "skills" / "web" / "skill.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        ans, results = m.research(q, llm)
        src = "\n".join("- " + r.get("url", "") for r in (results or [])[:3])
        return (ans + (f"\n\nSOURCES:\n{src}" if src else "")).strip()
    except Exception:
        return ""


def answer_one(llm):
    """Take the oldest open question and answer it — building understanding one question at a time,
    and CONFIRMING external facts on the live web rather than trusting training. Returns
    (question, answer) or None."""
    queue = _load_q()
    nxt = next((x for x in queue if not x.get("answered")), None)
    if not nxt:
        return None
    # Learning speed/quality knobs (config: explore.*). For fast bulk learning, route answers to a
    # cheap/fast role and optionally skip the per-answer red-team; defaults stay careful.
    try:
        from jarvis.config import load as _loadcfg
        _ex = (_loadcfg().get("explore", {}) or {})
    except Exception:
        _ex = {}
    role = _ex.get("answer_role", "researcher")
    ito = int(_ex.get("answer_inner_timeout", 150))
    do_verify = bool(_ex.get("verify", True))
    web = _web_evidence(nxt["q"], llm) if _looks_external(nxt["q"]) else ""
    prompt = ("You are building your own understanding of your environment. Investigate and answer this "
              "question as specifically as you can. Be skeptical: prefer the live WEB EVIDENCE and your "
              "NOTES over training-cutoff assumptions; if they conflict, trust the evidence and say so. "
              "If you truly cannot answer from what's known, state exactly what data you'd need.\n\n"
              f"QUESTION: {nxt['q']}\n\n"
              f"WEB EVIDENCE (live):\n{web or '(none gathered)'}\n\n"
              f"YOUR NOTES:\n{_known_context()}")
    ans = llm.run(role, prompt, timeout=ito).strip()
    # deep-fix discipline: red-team the answer (when explore.verify is on). Only REVISE when the check
    # actually ran AND broke it (verified=True, survives=False) — an un-run check (verified=False) must
    # not trigger churn, but it is recorded honestly as 'unverified' rather than silently trusted.
    verified = None
    if do_verify:
        try:
            from jarvis.config import load as _load
            from jarvis import verify as _verify
            ctx = _known_context()
            v = _verify.verify(_load(), ans, context=ctx)
            if v.get("verified") and not v.get("survives"):
                ans = llm.run(role,
                              f"Your draft answer was challenged by a red-team check. Critique:\n{v['critique']}\n\n"
                              f"QUESTION: {nxt['q']}\nRevise to honestly address it; if still unsure, say what's "
                              f"unverified.\n\nNOTES:\n{ctx}", timeout=ito).strip()
                v2 = _verify.verify(_load(), ans, context=ctx)
                verified = bool(v2.get("survives"))
            else:
                verified = bool(v.get("survives"))
        except Exception:
            verified = None

    # Don't file ignorance as knowledge: if the model says it can't answer, keep the question OPEN and
    # retry next cycle (discovery/web may fill the gap); after a few tries, escalate to the owner.
    unresolved = (len(ans) < 400 and bool(re.search(
        r"\b(cannot|can't|can not|unable to|don't know|do not know|insufficient|not enough|need (more|the|to)|unclear|no (information|data|way to))\b",
        ans.lower())))
    nxt["attempts"] = int(nxt.get("attempts", 0)) + 1
    if unresolved and nxt["attempts"] < 3:
        _save_q(queue)
        return nxt["q"], ans

    KNOW_DIR.mkdir(parents=True, exist_ok=True)
    note = KNOW_DIR / f"{_slug(nxt['q'])}.md"     # logical name per question, updated in place
    flag = "" if verified else "\n\n> ⚠ unverified — the red-team check did not confirm this."
    note.write_text(f"# Q: {nxt['q']}\n_answered {time.strftime('%Y-%m-%d %H:%M:%S')} · verified={verified}_\n\n{ans}\n{flag}")
    nxt["answered"], nxt["answer"], nxt["verified"] = True, ans[:400], verified
    _save_q(queue)

    if unresolved:                                # genuinely stuck -> ask the owner (close the human loop)
        try:
            from jarvis import messaging
            messaging.post_question(f"I couldn't resolve this on my own after {nxt['attempts']} tries: {nxt['q']}",
                                    ref="curiosity", conv="suggestions", title="Suggestions")
        except Exception:
            pass
    elif verified:                                # a good answer exposes the next unknown -> keep learning
        try:
            fu = llm.run("summarizer",
                         "From the answer below, list 0-3 SPECIFIC, non-duplicate follow-up questions that "
                         "would deepen understanding or resolve a stated unknown. One per line, no numbering. "
                         "If nothing is worth asking, output nothing.\n\nANSWER:\n" + ans[:1500], timeout=60)
            add_questions([re.sub(r"^[-*\d.\s]+", "", l).strip() for l in (fu or "").splitlines()
                           if len(l.strip()) > 12][:3])
        except Exception:
            pass
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
    # Include each file's mtime+size, not just COUNTS — discovery rewrites one doc per host IN PLACE,
    # so a count-only signature never changes on re-learn and suggest() would stop proposing work.
    parts = []
    for d in (DISC_DIR, KNOW_DIR):
        if d.exists():
            for p in sorted(d.glob("*.md")):
                try:
                    st = p.stat()
                    parts.append(f"{p.name}:{int(st.st_mtime)}:{st.st_size}")
                except Exception:
                    pass
    return "|".join(parts)


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


def generate_questions(llm, facts: dict) -> list:
    """Dedicated, format-robust question generation: ask the model for a plain list of concrete
    things worth investigating, ONE PER LINE. Does not depend on the discovery doc's prose
    formatting — relying on that left the queue empty, so Jarvis never had anything to learn."""
    blob = "\n\n".join(f"## {k}\n{v}" for k, v in facts.items() if v and not v.startswith("(unavailable"))
    prompt = ("From these RAW FACTS about a machine, list 6-10 SPECIFIC, concrete questions worth "
              "investigating to understand it better: unknowns, risks, things to verify, anything "
              "surprising. Output ONLY the questions, ONE PER LINE, no numbering, no bullets, no "
              "preamble, no markdown.\n\nRAW FACTS:\n" + blob[:12000])
    try:
        out = llm.run("summarizer", prompt, timeout=120) or ""
    except Exception:
        return []
    qs = []
    for line in out.splitlines():
        q = re.sub(r"^[\s\-\*\d.)]+", "", line).strip().strip("`*")
        if len(q) > 12:
            qs.append(q)
    return qs[:12]


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

    print("[discover] reading this host (services, ports, disks, network)…", flush=True)
    facts = scan_host()
    if a.network:
        print("[discover] sweeping the local subnet for other devices…", flush=True)
        net = scan_network()
        facts["network_scan"] = "\n".join(
            f"{h}: ports {', '.join(map(str, ports))}"
            for h, ports in sorted(net.items(), key=lambda kv: ipaddress.ip_address(kv[0]))
        ) or "(no responsive hosts/ports found)"
    route = ((cfg.get("llm", {}) or {}).get("routing", {}) or {}).get("orchestrator", "the brain")
    print(f"[discover] organizing findings with {route} (the model is thinking — up to ~240s)…", flush=True)
    doc = organize(llm, facts)

    DISC_DIR.mkdir(parents=True, exist_ok=True)
    host = facts.get("hostname", "host")
    path = DISC_DIR / f"{_slug(host)}.md"        # ONE stable, logically-named doc per host — updated in place
    path.write_text(f"# Discovery: {host}\n_updated {time.strftime('%Y-%m-%d %H:%M:%S')}_\n\n{doc}\n")
    # Queue questions from BOTH the doc (if it had a parseable list) and a dedicated structured
    # call (format-robust); add_questions dedupes. This is what keeps the curiosity engine fed.
    qs = extract_questions(doc) + generate_questions(llm, facts)
    add_questions(qs)
    write_index()
    print(f"[discover] updated {path} — queued {len(qs)} candidate question(s)", flush=True)
    if a.print:
        print("\n" + doc)


if __name__ == "__main__":
    main()
