#!/usr/bin/env python3
"""web — let Jarvis browse the live web so it isn't stuck at its model's training cutoff.

  fetch <url>          -> readable text of a page
  search <query>       -> top results (title + url) via DuckDuckGo (no API key)
  research <question>  -> search + fetch top results + the brain answers from CURRENT info, with sources

Stdlib + the Jarvis LLM router. Degrade-safe: network/parse errors return empty, never crash.
"""
from __future__ import annotations
import argparse, html, re, sys, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

UA = "Mozilla/5.0 (X11; Linux x86_64) Jarvis/0.1"


def _get(url, data=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _text(markup):
    markup = re.sub(r"(?is)<(script|style|nav|footer|header|noscript).*?</\1>", " ", markup)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"(?s)<[^>]+>", " ", markup))).strip()


def fetch(url, cap=6000):
    try:
        return _text(_get(url))[:cap]
    except Exception as e:
        return f"(fetch failed: {str(e)[:80]})"


def search(query, n=6):
    """DuckDuckGo HTML results (no key). Returns [{title, url}]."""
    try:
        body = urllib.parse.urlencode({"q": query}).encode()
        page = _get("https://html.duckduckgo.com/html/", data=body)
    except Exception:
        return []
    out = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, re.S):
        href, title = m.group(1), _text(m.group(2))
        if "uddg=" in href:                       # decode DDG's redirect wrapper
            mm = re.search(r"uddg=([^&]+)", href)
            if mm:
                href = urllib.parse.unquote(mm.group(1))
        if title and href.startswith("http"):
            out.append({"title": title, "url": href})
        if len(out) >= n:
            break
    return out


def research(question, llm):
    results = search(question, 5)
    corpus = "".join(f"\n\n=== {r['title']} ({r['url']}) ===\n{fetch(r['url'], 3000)}" for r in results[:3])
    prompt = ("Answer the question using these CURRENT web results (real, up-to-date info — trust "
              "them over your training data). Cite the source URLs. If they don't answer it, say so.\n\n"
              f"QUESTION: {question}\n\nWEB RESULTS:\n{corpus[:12000]}")
    return llm.run("researcher", prompt, timeout=180).strip(), results


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch"); f.add_argument("url")
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("-n", type=int, default=6)
    r = sub.add_parser("research"); r.add_argument("question")
    a = ap.parse_args()
    if a.cmd == "fetch":
        print(fetch(a.url))
    elif a.cmd == "search":
        for x in search(a.query, a.n):
            print(f"- {x['title']}\n  {x['url']}")
    elif a.cmd == "research":
        from jarvis.config import load
        from jarvis.adapters.llm import build_llm
        llm = build_llm(load())
        if not llm:
            sys.exit("web: no brain configured")
        ans, results = research(a.question, llm)
        print(ans + "\n\nSources:\n" + "\n".join(" - " + x["url"] for x in results))


if __name__ == "__main__":
    main()
