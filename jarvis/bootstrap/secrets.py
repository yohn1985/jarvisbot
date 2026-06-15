"""Light secrets store (Phase 0).

Minimal foundation: upserts secrets into the gitignored .env (chmod 600) and loads .env into the
process environment so the LLM router and CLIs can see them (e.g. OLLAMA_API_KEY). This is
hygiene-grade — keeps keys out of source/logs/chat — not a fortress. Encryption-at-rest (Fernet)
and a pluggable external-vault adapter are planned upgrades; the interface here stays the same.

Stdlib only.
"""
from __future__ import annotations
import os, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
ENV = ROOT / ".env"


def load_env() -> None:
    """Load non-empty KEY=VALUE pairs from .env into the environment (without overriding values
    already set). Skips blanks/comments and empty values so an empty .env.example key like
    `ANTHROPIC_API_KEY=` never shadows real auth."""
    if not ENV.exists():
        return
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and v:
            os.environ.setdefault(k, v)


def unset(name: str) -> None:
    """Remove a secret from .env and the current process env (used to clean a bad credential)."""
    name = name.strip()
    if ENV.exists():
        kept = [ln for ln in ENV.read_text().splitlines() if not re.match(rf"\s*{re.escape(name)}\s*=", ln)]
        ENV.write_text("\n".join(kept) + ("\n" if kept else ""))
    os.environ.pop(name, None)


def set_secret(name: str, value: str) -> None:
    """Upsert a secret into .env (chmod 600) and into the current process env."""
    name = name.strip()
    lines, found = [], False
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            if re.match(rf"\s*{re.escape(name)}\s*=", line):
                lines.append(f"{name}={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{name}={value}")
    ENV.write_text("\n".join(lines) + "\n")
    try:
        os.chmod(ENV, 0o600)
    except Exception:
        pass
    os.environ[name] = value
