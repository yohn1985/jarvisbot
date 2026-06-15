---
name: web
description: Browse the live web — fetch pages, search (no API key), and answer questions from current info so the brain isn't limited to its training cutoff.
entrypoint: skill.py
requires: (none — stdlib + the Jarvis LLM router)
tools: urllib (HTTP), DuckDuckGo HTML, the LLM router
when_to_use: a question depends on CURRENT facts (releases, versions, prices, news, docs) or the brain is unsure / might be out of date — verify against the live web instead of trusting training data.
---

# web

Gives Jarvis eyes on the live internet so it can check reality instead of guessing from stale
training data (e.g. "is Ubuntu 26.04 out yet?").

```
fetch <url>          readable text of a page
search <query>       top results (title + url), DuckDuckGo, no API key
research <question>  search -> fetch top results -> the brain answers from CURRENT info, with sources
```

Degrade-safe: any network/parse failure returns empty rather than crashing. The `research` answer is
instructed to trust the fetched results over training data and to cite source URLs.

## Usage
```
./install.sh skill run web search "ubuntu 26.04 release date"
./install.sh skill run web research "is ubuntu 26.04 released and what's new"
./install.sh skill run web fetch https://example.com
```
