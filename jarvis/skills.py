#!/usr/bin/env python3
"""Skill registry: discover skills by scanning skills/*/SKILL.md frontmatter.
A skill is a self-contained folder (manifest + code + own requirements), so the
skills/ tree is a modular, extensible repo — drop in a folder, it's a new capability.
The mind lists skills (cheap), reads a SKILL.md just-in-time, and invokes the entrypoint."""
import os, sys, re, json
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
# Skills come from the core skills/, the private overlay extras/skills/, AND the runtime
# state/skills/ where operator/AI-added skills are written. state/ is NOT overwritten on a
# reinstall (install.sh copies the repo over /opt/jarvis), so user skills persist there.
USER_SKILL_DIR = ROOT / "state" / "skills"
SKILL_DIRS = [ROOT / "skills", Path(os.environ.get("JARVIS_EXTRAS", ROOT / "extras")) / "skills", USER_SKILL_DIR]

def _frontmatter(md: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---", md, re.S); fm = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1); fm[k.strip()] = v.strip()
    return fm

def list_skills() -> list:
    out = []
    for base in SKILL_DIRS:
        for d in sorted(base.glob("*/SKILL.md")):
            fm = _frontmatter(d.read_text()); fm["dir"] = str(d.parent)
            fm.setdefault("name", d.parent.name); out.append(fm)
    return out


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s[:48]


def save_skill(name: str, content: str) -> dict:
    """Write a skill (the SKILL.md text) into the persistent state/skills/<slug>/ dir so it's
    discovered by list_skills() and survives reinstalls. Used by the dashboard '+ Add skill'
    button and the add_skill chat tool. If the pasted text has no YAML frontmatter, a minimal
    one is added so the skill is detectable (name + a description from the first line)."""
    slug = _slug(name)
    if not slug:
        return {"ok": False, "error": "a skill name is required"}
    body = (content or "").strip()
    if not body:
        return {"ok": False, "error": "skill text is empty"}
    fm = _frontmatter(body)
    if not fm:                                  # no frontmatter -> synthesize a minimal one
        first = next((ln.strip().lstrip("# ").strip() for ln in body.splitlines() if ln.strip()), slug)
        body = f"---\nname: {slug}\ndescription: {first[:160]}\n---\n\n{body}"
    elif "name" not in fm:                       # has frontmatter but no name -> inject it
        body = body.replace("---", f"---\nname: {slug}", 1)
    try:
        dest = USER_SKILL_DIR / slug
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "SKILL.md").write_text(body)
        return {"ok": True, "name": slug, "path": str(dest / "SKILL.md")}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def catalog(limit: int = 40) -> str:
    """A compact catalog of available skills for prompt injection so the model can DETECT and
    use skills on its own: name, what it's for, when to use it, and where its full instructions
    (and any script it references) live — which the model reads/runs via the read/shell tools."""
    lines = []
    for s in list_skills()[:limit]:
        nm = s.get("name", "")
        desc = s.get("description", "")
        when = s.get("when_to_use", "")
        ref = f"{s.get('dir','')}/SKILL.md"
        extra = ""
        for k in ("script", "utility", "entrypoint"):
            if s.get(k):
                extra = f", script: {s[k]}"
                break
        line = f"- {nm} — {desc}".rstrip(" —")
        if when:
            line += f" When: {when}"
        line += f" (instructions: {ref}{extra})"
        lines.append(line)
    return "\n".join(lines)


if __name__ == "__main__":
    if sys.argv[1:2] == ["list"]:
        for s in list_skills():
            print(f"  {s.get('name'):22} {s.get('description','')}")
    else:
        print(json.dumps(list_skills(), indent=2))
