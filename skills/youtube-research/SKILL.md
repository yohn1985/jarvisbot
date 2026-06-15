---
name: youtube-research
description: Research a strategy/market question from many YouTube videos and return a decision-led report (not summaries).
entrypoint: skill.py
requires: yt-dlp
tools: yt-dlp, jq, rg, python3
when_to_use: the user wants market/sales/product/competitor research compressed into a recommendation, based on 5-15 videos where current creator claims/tactics matter.
---

# youtube-research

**Ported from the owner's canonical runbook** (`work/docs/runbooks/youtube-research.md`). The
value is the *method and judgment*, not transcript dumps. `skill.py` provides the mechanical
tools (search, caption download, VTT cleaning, chunked reading); the mind supplies the synthesis.

## Goal
Search the right topic → pick relevant videos → read transcripts → compare patterns across
sources → return a clear recommendation + 30-day test plan. A decision, not a book report.

## Ground rules (do not relax)
- Read the FULL cleaned transcript for every selected video before concluding. Keyword extraction
  is orientation only, never a substitute for the full read.
- Treat creator claims as claims, not facts. Separate repeated patterns from one-off claims.
- Don't overquote — summarize in your own words and link sources.
- Note when visual review was not performed (transcripts ≠ watching the frames).

## Workflow
1. **Define the question** in one plain sentence + what the final decision must answer.
2. **Search in clusters** (4-8 angle queries) to avoid a single-creator biased sample.
3. **Build a candidate list** (12-20 → pick best 10-15): include competing viewpoints, prefer
   concrete process over motivation, avoid same-channel duplicates, prefer recent when tactics matter.
4. **Download transcripts** — `skill.py download <video_id>` (yt-dlp auto+manual captions → vtt).
5. **Clean captions** — `skill.py clean` (vtt → de-duplicated plain text).
6. **Read every transcript end-to-end** — `skill.py chunk` (500-word chunks). Note per video:
   what they sell, to whom, offer/pricing, funnel, targeting, follow-up, metrics claimed,
   what's useful, what's inflated, how it applies to us.
7. **Extract evidence by theme** (rg/keyword snippets) — cross-check repeated patterns only.
8. **Score each video** — the load-bearing field is "How it applies to us."
9. **Compare patterns** by theme (paid ads / cold email / maps / loom / niche / offer / funnel /
   follow-up / deliverability): agree? disagree? inflated? directly useful?
10. **Write the report**: Short answer · What I reviewed · Patterns · What I believe is true ·
    What I'd do · 30-day test plan · Metrics to watch · What not to do · Sources.

## Quality checklist
≥10 relevant videos read end-to-end; every source has a URL; claims separated from facts; not
reliant on one creator/tactic; clear recommendation + practical test plan; no overquoting; visual
gaps noted. If the unit economics don't support the CAC, say so plainly.

## Usage (mechanical tools)
```
./install.sh skill install youtube-research
./install.sh skill run youtube-research search "cold email agency clients" -n 12
./install.sh skill run youtube-research download VIDEO_ID
./install.sh skill run youtube-research clean
./install.sh skill run youtube-research chunk
```
