---
name: discover
description: Autodiscovery — scan the machine (host, services, ports, network neighbours, containers) and have the brain write documentation about it, the foundation of Jarvis understanding its world.
entrypoint: skill.py
requires: (none beyond ripgrep/standard linux tools)
tools: systemctl, ss, ip, df, docker (all optional/degrade-safe), the Jarvis LLM router
when_to_use: Jarvis has landed somewhere new (or its understanding of the environment is stale) and needs to learn and document what this machine is, what it runs, and what's around it.
---

# discover

The heart of Jarvis: land on a machine, look around, and **write down what you learned** so it
becomes durable understanding instead of a one-off glance.

```
SCAN (pure Python/subprocess)  → bounded raw facts about the host + what it can see
ORGANIZE (LLM)                 → the brain turns raw facts into a clear, factual document
PERSIST                        → workspace/discovery/<host>-<ts>.md
```

Scanning is degrade-safe (any missing tool is skipped, never crashes). The organizing step runs on
Jarvis's configured brain (claude/codex/ollama-cloud) — it must not invent anything not in the facts.

## Usage
```
./install.sh skill run discover            # discover this host, write the doc
./install.sh skill run discover --print    # also print the document
```

## Output
A markdown document describing the machine's role, services + ports, networking (interfaces,
routes, neighbours/other devices), storage, containers, and an "open questions / worth
investigating" list. Saved under `workspace/discovery/`.

## Roadmap (next increments)
- Network sweep: enumerate other devices/ports on the local subnet (consent-gated).
- Persist findings into the `knowledge`/`knowledge_map` memory tables (staleness drives curiosity).
- Loop integration: run on the curiosity rung so Jarvis keeps its understanding fresh on its own.
