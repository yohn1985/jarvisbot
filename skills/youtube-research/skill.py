#!/usr/bin/env python3
"""
youtube-research — mechanical tools for the owner's YouTube research runbook.

The SKILL.md holds the METHOD (the judgment). This file is just the hands: search,
download captions, clean VTT, chunked reading — ported verbatim-in-spirit from the
canonical runbook (work/docs/runbooks/youtube-research.md). The synthesis/decision is
the mind's job, following SKILL.md.

    python skill.py search "<query>" [-n 12]
    python skill.py download <video_id> [<video_id> ...]
    python skill.py clean
    python skill.py chunk [--size 500]

Scratch lives OUTSIDE any repo: /tmp/youtube_research
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys
from pathlib import Path

SCRATCH = Path("/tmp/youtube_research")


def _bin(tool: str):
    """Resolve a tool from THIS interpreter's venv bin first, then PATH. Install puts yt-dlp in
    .venv/bin, which isn't on PATH when the skill runs as `.venv/bin/python skill.py` — so a plain
    which() wrongly reported it 'not installed' even after a successful install."""
    cand = os.path.join(os.path.dirname(sys.executable), tool)
    return cand if os.path.exists(cand) else shutil.which(tool)


def _need(tool: str) -> str:
    b = _bin(tool)
    if not b:
        sys.exit(f"{tool} not installed — open the Skills tab and click Install "
                 f"(or run: ./install.sh skill install youtube-research)")
    return b


def search(query: str, n: int = 12) -> list[dict]:
    """Candidate videos for a cluster query (no API key). Capture id/title/channel/url."""
    ytdlp = _need("yt-dlp")
    proc = subprocess.run(
        [ytdlp, f"ytsearch{n}:{query}", "--dump-json", "--flat-playlist", "--no-warnings"],
        capture_output=True, text=True,
    )
    out = []
    for line in proc.stdout.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        vid = d.get("id")
        out.append({"id": vid, "title": d.get("title"),
                    "channel": d.get("channel") or d.get("uploader"),
                    "url": d.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else None),
                    "found_by": query})
    return out


def download(video_ids: list[str]) -> None:
    """Auto + manual English captions -> vtt in the scratch folder."""
    ytdlp = _need("yt-dlp")
    SCRATCH.mkdir(parents=True, exist_ok=True)
    for vid in video_ids:
        subprocess.run([
            ytdlp, "--skip-download", "--write-subs", "--write-auto-subs",
            "--sub-lang", "en", "--convert-subs", "vtt",
            "-o", str(SCRATCH / "%(id)s.%(ext)s"),
            f"https://www.youtube.com/watch?v={vid}",
        ])
    print(f"[youtube-research] captions in {SCRATCH}")


def clean() -> None:
    """VTT -> readable de-duplicated plain text (.clean.txt). Ported from the runbook."""
    for path in SCRATCH.glob("*.vtt"):
        lines = []
        for line in path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line == "WEBVTT" or "-->" in line or re.match(r"^\d+$", line):
                continue
            line = re.sub(r"<[^>]+>", "", line)
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                lines.append(line)
        words, cleaned, last = " ".join(lines).split(), [], None
        for w in words:
            if w != last:
                cleaned.append(w)
            last = w
        out = path.with_suffix("").with_suffix(".clean.txt")
        out.write_text(" ".join(cleaned) + "\n")
        print(out, len(cleaned), "words")


def chunk(size: int = 500) -> None:
    """Print every cleaned transcript in sequential chunks for an end-to-end read."""
    for path in sorted(SCRATCH.glob("*.clean.txt")):
        words = path.read_text(errors="ignore").split()
        print(f"\n\n===== VIDEO {path.name} WORDS {len(words)} =====")
        for i in range(0, len(words), size):
            print(f"\n--- {path.name} chunk {i // size + 1} words {i}-{min(i + size, len(words))} ---")
            print(" ".join(words[i:i + size]))


def main():
    ap = argparse.ArgumentParser(description="youtube-research mechanical tools")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("-n", type=int, default=12)
    d = sub.add_parser("download"); d.add_argument("video_ids", nargs="+")
    sub.add_parser("clean")
    c = sub.add_parser("chunk"); c.add_argument("--size", type=int, default=500)
    a = ap.parse_args()
    if a.cmd == "search":
        for v in search(a.query, a.n):
            print(f"  {v['id']}  {v['title']}  —  {v['channel']}\n     {v['url']}")
    elif a.cmd == "download":
        download(a.video_ids)
    elif a.cmd == "clean":
        clean()
    elif a.cmd == "chunk":
        chunk(a.size)


if __name__ == "__main__":
    main()
