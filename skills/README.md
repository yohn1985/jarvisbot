# Jarvis skills

A modular, extensible capability repo. Each skill is a self-contained folder:

```
skills/<name>/
  SKILL.md          # manifest (frontmatter) + when-to-use + usage
  skill.py          # entrypoint (CLI: python skill.py <args>)
  requirements.txt  # the skill's OWN deps (core stays lean)
```

`SKILL.md` frontmatter:
```
---
name: <name>
description: <one line — used by the mind to decide relevance>
entrypoint: skill.py
requires: <comma-separated pip deps>
when_to_use: <when the mind should reach for this>
---
```

Manage skills:
```
./install.sh skill list
./install.sh skill install <name>     # deps into the project .venv
./install.sh skill run <name> [args]
```

A skill should be runnable standalone (for testing) AND callable by the kernel.
