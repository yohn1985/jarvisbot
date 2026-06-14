#!/usr/bin/env python3
"""Skill registry: discover skills by scanning skills/*/SKILL.md frontmatter.
A skill is a self-contained folder (manifest + code + own requirements), so the
skills/ tree is a modular, extensible repo — drop in a folder, it's a new capability.
The mind lists skills (cheap), reads a SKILL.md just-in-time, and invokes the entrypoint."""
import sys, re, json
from pathlib import Path
SKILLS = Path(__file__).resolve().parent.parent / "skills"

def _frontmatter(md: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---", md, re.S); fm = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1); fm[k.strip()] = v.strip()
    return fm

def list_skills() -> list:
    out = []
    for d in sorted(SKILLS.glob("*/SKILL.md")):
        fm = _frontmatter(d.read_text()); fm["dir"] = str(d.parent)
        fm.setdefault("name", d.parent.name); out.append(fm)
    return out

if __name__ == "__main__":
    if sys.argv[1:2] == ["list"]:
        for s in list_skills():
            print(f"  {s.get('name'):22} {s.get('description','')}")
    else:
        print(json.dumps(list_skills(), indent=2))
