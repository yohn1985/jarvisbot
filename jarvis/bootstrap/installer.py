"""Pure-Python self-installer (Phase 0, step 2) — DETECTION + PLAN only.

This module is READ-ONLY: it inspects the machine and produces an itemized PLAN of everything
Jarvis needs to set up to become operational — its whole "workshop": venv, Python deps, system
tools, folders, config files, the AI CLIs, shipped-skill deps, and (opt-in) the long-term
memory database + semantic/vector store. It NEVER executes anything here — execution happens
only after the owner approves the plan through the consent engine (next step). No AI involved.

    detect(cfg) -> facts about this machine
    plan(cfg)   -> [ {id, desc, cmd, needs_sudo, reversible, blocked, blocked_reason} ... ]
                   (only the actions for things that are MISSING / opted-in)

Install commands are config-overridable (bootstrap.*) so the public core hardcodes no opinion;
the defaults are sane and shown to you in the plan before anything ever runs. Stdlib only.
"""
from __future__ import annotations
import os, shutil, subprocess, sys
from pathlib import Path

from jarvis.bootstrap import preflight

ROOT = Path(__file__).resolve().parent.parent.parent

# Distro package managers, in preference order: (binary, install-subcommand).
_PKG_MANAGERS = [
    ("apt-get", ["apt-get", "install", "-y"]),
    ("dnf",     ["dnf", "install", "-y"]),
    ("pacman",  ["pacman", "-S", "--noconfirm"]),
    ("zypper",  ["zypper", "install", "-y"]),
    ("apk",     ["apk", "add"]),
    ("brew",    ["brew", "install"]),
]

# Python runtime deps: (import_name, pip_spec).
_PYDEPS = [("yaml", "pyyaml"), ("psycopg", "psycopg[binary]"), ("redis", "redis")]

# Core system tools the agent itself relies on: (binary, package-name). Per-skill tools (yt-dlp,
# nmap, ...) are NOT here — they come with the skill that needs them, to keep a fresh box lean.
_CORE_TOOLS = [("rg", "ripgrep"), ("git", "git"), ("curl", "curl"), ("jq", "jq")]

# Working folders the agent expects (workspace = where Phase 1 writes the docs it gathers).
_DIRS = ["state", "tasks", "output", "workspace"]

# OS package name for Node across managers (brew calls it "node", others "nodejs"+"npm").
_NODE_PKGS = {"brew": ["node"]}
_NODE_PKGS_DEFAULT = ["nodejs", "npm"]

# Defaults shown to you before any install (override via cfg.bootstrap.*).
_DEFAULT_CLI_PACKAGES = {"claude": "@anthropic-ai/claude-code", "codex": "@openai/codex"}
_DEFAULT_SEMANTIC_PACKAGES = {"sqlite-vec": ["sqlite-vec"], "chroma": ["chromadb"]}


def _venv_py() -> str:
    return str(ROOT / ".venv" / "bin" / "python")   # the project venv is the install target


def _pkg_manager():
    for name, base in _PKG_MANAGERS:
        if shutil.which(name):
            return name, base
    return None, None


def _norm(spec: str) -> str:
    """pip spec -> normalized package name: 'psycopg[binary]>=3.1' -> 'psycopg'."""
    for sep in ("[", "==", ">=", "<=", "~=", ">", "<", "!=", ";", " "):
        spec = spec.split(sep)[0]
    return spec.strip().lower().replace("_", "-")


def _venv_installed():
    """Lowercased set of pip packages installed in the project venv; None if there's no venv yet
    (so deps read as 'missing' until the venv exists). Deps install into the venv, not system
    python, so this is what we must check for idempotency."""
    py = _venv_py()
    if not Path(py).exists():
        return None
    try:
        out = subprocess.run([py, "-m", "pip", "list", "--format=freeze"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return set()
    return {_norm(line) for line in out.splitlines() if line.strip()}


def _missing_pydeps(installed) -> list[str]:
    if installed is None:                       # no venv yet -> venv action runs first, then these
        return [spec for _, spec in _PYDEPS]
    return [spec for _, spec in _PYDEPS if _norm(spec) not in installed]


def _skill_satisfied(req: Path, installed) -> bool:
    if installed is None:
        return False
    for line in req.read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#") and _norm(s) not in installed:
            return False
    return True


def _has_real_reqs(p: Path) -> bool:
    return p.exists() and any(s.strip() and not s.strip().startswith("#") for s in p.read_text().splitlines())


def _shipped_skills() -> list[str]:
    base = ROOT / "skills"
    return sorted(d.name for d in base.glob("*") if (d / "requirements.txt").exists()) if base.exists() else []


def _cfg_get(cfg: dict, *path, default=None):
    cur = cfg
    for k in path:
        cur = (cur or {}).get(k) if isinstance(cur, dict) else None
    return cur if cur is not None else default


def _cli_packages(cfg: dict) -> dict:
    return {**_DEFAULT_CLI_PACKAGES, **(_cfg_get(cfg, "bootstrap", "cli_packages", default={}) or {})}


def detect(cfg: dict) -> dict:
    pm_name, _ = _pkg_manager()
    is_root = (os.geteuid() == 0) if hasattr(os, "geteuid") else False
    installed = _venv_installed()
    return {
        "pkg_manager": pm_name,
        "venv": Path(_venv_py()).exists(),
        "venv_installed": installed,
        "node": bool(shutil.which("node")),
        "npm": bool(shutil.which("npm")),
        "docker": bool(shutil.which("docker")),
        "missing_pydeps": _missing_pydeps(installed),
        "missing_tools": [(b, pkg) for b, pkg in _CORE_TOOLS if not shutil.which(b)],
        "missing_dirs": [d for d in _DIRS if not (ROOT / d).exists()],
        "have_env": (ROOT / ".env").exists(),
        "have_config": (ROOT / "config.yaml").exists(),
        "ai_clis": preflight.find_ai_clis(cfg),            # {backend: abspath} already-present brains
        "shipped_skills": _shipped_skills(),
        "provision_database": bool(_cfg_get(cfg, "bootstrap", "provision_database", default=False)),
        "semantic_kind": _cfg_get(cfg, "memory", "semantic", "kind", default="none"),
        "is_root": is_root,
        "can_sudo": bool(shutil.which("sudo")),
    }


def _action(d, *, id, desc, cmd, needs_sudo=False, reversible=True, blocked=False, reason=""):
    if needs_sudo and not d["is_root"]:
        if d["can_sudo"]:
            cmd = ["sudo"] + cmd
        else:
            blocked, reason = True, "needs root/sudo, neither available"
    return {"id": id, "desc": desc, "cmd": cmd, "needs_sudo": needs_sudo,
            "reversible": reversible, "blocked": blocked, "blocked_reason": reason}


# --- per-category plan builders (each returns 0+ actions; only for MISSING/opted-in things) ---

def _plan_venv(d):
    if d["venv"]:
        return []
    acts = []
    pm_name, pm_base = _pkg_manager()
    if pm_name == "apt-get":   # Debian/Ubuntu split venv + pip out of the base python package
        acts.append(_action(d, id="python_base", desc="Install Python venv+pip support (python3-venv, python3-pip)",
                            cmd=pm_base + ["python3-venv", "python3-pip"], needs_sudo=True))
    acts.append(_action(d, id="venv", desc="Create the project virtualenv (.venv)",
                        cmd=[sys.executable, "-m", "venv", str(ROOT / ".venv")]))
    return acts


def _plan_pydeps(d):
    if not d["missing_pydeps"]:
        return []
    req = ROOT / "requirements.txt"
    cmd = [_venv_py(), "-m", "pip", "install"] + (["-r", str(req)] if req.exists() else d["missing_pydeps"])
    return [_action(d, id="pydeps", desc=f"Install Python deps ({', '.join(d['missing_pydeps'])})", cmd=cmd)]


def _plan_tools(d):
    if not d["missing_tools"]:
        return []
    _, pm_base = _pkg_manager()
    pkgs = [pkg for _, pkg in d["missing_tools"]]
    if not pm_base:
        return [_action(d, id="tools", desc=f"Install system tools ({', '.join(pkgs)})", cmd=[],
                        needs_sudo=True, blocked=True, reason="no supported package manager detected")]
    return [_action(d, id="tools", desc=f"Install system tools ({', '.join(pkgs)})",
                    cmd=pm_base + pkgs, needs_sudo=True)]


def _plan_folders(d):
    if not d["missing_dirs"]:
        return []
    return [_action(d, id="folders", desc=f"Create working folders ({', '.join(d['missing_dirs'])})",
                    cmd=["mkdir", "-p"] + [str(ROOT / x) for x in d["missing_dirs"]])]


def _plan_config(d):
    actions = []
    if not d["have_config"]:
        actions.append(_action(d, id="config", desc="Create config.yaml from the example",
                               cmd=["cp", str(ROOT / "config.example.yaml"), str(ROOT / "config.yaml")]))
    if not d["have_env"]:
        actions.append(_action(d, id="env", desc="Create .env from the example (then add secrets)",
                               cmd=["cp", str(ROOT / ".env.example"), str(ROOT / ".env")]))
    return actions


def _plan_brain(d, cfg):
    if d["ai_clis"]:                       # a brain already exists — nothing to install
        return []
    actions = []
    pm_name, pm_base = _pkg_manager()
    if not (d["node"] and d["npm"]):
        if pm_base:
            actions.append(_action(d, id="node", desc="Install Node.js + npm (needed to install the AI CLIs)",
                                   cmd=pm_base + _NODE_PKGS.get(pm_name, _NODE_PKGS_DEFAULT), needs_sudo=True))
        else:
            actions.append(_action(d, id="node", desc="Install Node.js + npm", cmd=[], needs_sudo=True,
                                   blocked=True, reason="no supported package manager detected"))
    pkgs = _cli_packages(cfg)
    for backend in ("claude", "codex"):    # prefer one brain; owner can add the other later
        if backend in pkgs:
            actions.append(_action(d, id=f"cli_{backend}",
                                   desc=f"Install the {backend} CLI ({pkgs[backend]}) via npm",
                                   cmd=["npm", "install", "-g", pkgs[backend]]))
            break
    return actions


def _plan_skills(d):
    actions = []
    for name in d["shipped_skills"]:
        req = ROOT / "skills" / name / "requirements.txt"
        # skip comment-only manifests (e.g. deep-search) and skills already satisfied in the venv
        if _has_real_reqs(req) and not _skill_satisfied(req, d["venv_installed"]):
            actions.append(_action(d, id=f"skill_{name}", desc=f"Install deps for shipped skill '{name}'",
                                   cmd=[_venv_py(), "-m", "pip", "install", "-r", str(req)]))
    return actions


def _plan_database(d):
    if not d["provision_database"]:        # opt-in only — default is file-based memory
        return []
    compose = str(ROOT / "docker-compose.yml")
    if not d["docker"]:
        return [_action(d, id="database", desc="Stand up Postgres + Redis (long-term memory)", cmd=[],
                        blocked=True, reason="Docker not found — install Docker or keep file-based memory")]
    return [
        _action(d, id="db_up", desc="Start Postgres + Redis (docker compose up)",
                cmd=["docker", "compose", "-f", compose, "up", "-d"], reversible=True),
        _action(d, id="db_init", desc="Apply the memory schema (db/schema.sql)",
                cmd=[str(ROOT / "install.sh"), "initdb"], reversible=True),
        _action(d, id="db_migrate", desc="Import existing ledger -> episodes",
                cmd=[str(ROOT / "install.sh"), "migrate"], reversible=True),
    ]


def _plan_semantic(d, cfg):
    kind = d["semantic_kind"]
    if not kind or kind == "none":         # opt-in — default off (day-1 uses no semantic layer)
        return []
    pkgs = {**_DEFAULT_SEMANTIC_PACKAGES, **(_cfg_get(cfg, "bootstrap", "semantic_packages", default={}) or {})}
    spec = pkgs.get(kind)
    if not spec:
        return [_action(d, id="semantic", desc=f"Set up semantic/vector memory ('{kind}')", cmd=[],
                        blocked=True, reason=f"no install command known for semantic kind '{kind}'")]
    return [_action(d, id="semantic", desc=f"Install vector/semantic memory deps ({', '.join(spec)})",
                    cmd=[_venv_py(), "-m", "pip", "install"] + spec)]


def plan(cfg: dict) -> list[dict]:
    """The full ordered workshop plan — only actions for what's MISSING / opted-in. [] = ready."""
    d = detect(cfg)
    out: list[dict] = []
    out += _plan_venv(d)
    out += _plan_pydeps(d)
    out += _plan_tools(d)
    out += _plan_folders(d)
    out += _plan_config(d)
    out += _plan_brain(d, cfg)
    out += _plan_skills(d)
    out += _plan_database(d)
    out += _plan_semantic(d, cfg)
    # Fresh boxes have a stale/empty apt cache — refresh first if anything will apt-install.
    pm_name, _ = _pkg_manager()
    if pm_name == "apt-get" and any(c for a in out for c in [a["cmd"]] if "apt-get" in c):
        out.insert(0, _action(d, id="apt_update", desc="Refresh package lists (apt-get update)",
                              cmd=["apt-get", "update"], needs_sudo=True))
    return out


def describe(cfg: dict) -> str:
    d = detect(cfg)
    p = plan(cfg)
    lines = [
        "Machine: pkg_manager=%s node=%s npm=%s docker=%s root=%s sudo=%s"
        % (d["pkg_manager"], d["node"], d["npm"], d["docker"], d["is_root"], d["can_sudo"]),
        "venv=%s  config.yaml=%s  .env=%s" % (d["venv"], d["have_config"], d["have_env"]),
        "AI brains present: %s" % (", ".join(d["ai_clis"]) or "none"),
        "Missing python deps: %s" % (", ".join(d["missing_pydeps"]) or "none"),
        "Missing tools: %s" % (", ".join(b for b, _ in d["missing_tools"]) or "none"),
        "Shipped skills: %s | DB opt-in=%s | semantic=%s" % (
            ", ".join(d["shipped_skills"]) or "none", d["provision_database"], d["semantic_kind"]),
        "",
    ]
    if not p:
        lines.append("Workshop plan: nothing to do — already operational.")
    else:
        lines.append("Workshop plan (%d action%s) — NOTHING runs until approved:"
                     % (len(p), "" if len(p) == 1 else "s"))
        for a in p:
            mark = "  [BLOCKED: %s]" % a["blocked_reason"] if a.get("blocked") else \
                   ("  [sudo]" if a["needs_sudo"] else "")
            lines.append("  - %s%s\n      $ %s" % (a["desc"], mark, " ".join(a["cmd"]) or "(no command)"))
    return "\n".join(lines)


if __name__ == "__main__":
    from jarvis.config import load
    print(describe(load()))
