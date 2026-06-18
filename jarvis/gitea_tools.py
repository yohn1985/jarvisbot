"""Config-driven Gitea probes for Jarvis chat evidence.

The chat harness uses this module for deterministic ticket lookups without
assuming a product repo. Gitea location and token source come from config.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from jarvis.config import load


def _worksource() -> dict:
    return (load().get("worksource") or {})


def _api_base(ws: dict) -> str:
    base = os.environ.get("JARVIS_GITEA_API") or ws.get("api") or ""
    return base.rstrip("/")


def _token(ws: dict) -> str:
    direct = os.environ.get("JARVIS_GITEA_TOKEN")
    if direct:
        return direct.strip()
    cmd = os.environ.get("JARVIS_GITEA_TOKEN_CMD") or ws.get("token_cmd") or ""
    if not cmd:
        return ""
    parts = shlex.split(str(cmd))
    if not parts:
        return ""
    return subprocess.run(parts, capture_output=True, text=True, timeout=10).stdout.strip()


def _client() -> tuple[str, str]:
    ws = _worksource()
    base = _api_base(ws)
    token = _token(ws)
    if not base:
        raise RuntimeError("Gitea API is not configured in worksource.api")
    if not token:
        raise RuntimeError("Gitea token is not configured in worksource.token_cmd or JARVIS_GITEA_TOKEN")
    return base, token


def _get_json(base: str, token: str, path: str):
    req = urllib.request.Request(
        base + path,
        headers={"Authorization": f"token {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def _repo_path(full_name: str) -> str:
    owner, name = full_name.split("/", 1)
    return f"{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"


def iter_repos(base: str, token: str):
    page = 1
    while True:
        repos = _get_json(base, token, f"/user/repos?limit=50&page={page}")
        if not isinstance(repos, list) or not repos:
            break
        for repo in repos:
            full_name = repo.get("full_name")
            if full_name and "/" in full_name:
                yield full_name
        if len(repos) < 50:
            break
        page += 1


def issue_number(number: int) -> int:
    base, token = _client()
    matches = []
    checked = 0
    for full_name in iter_repos(base, token):
        checked += 1
        try:
            issue = _get_json(base, token, f"/repos/{_repo_path(full_name)}/issues/{number}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        matches.append((full_name, issue))

    matches.sort(key=lambda item: (
        bool(item[1].get("pull_request")),
        item[1].get("state") != "open",
        item[0],
    ))
    open_issue_matches = sum(
        1 for _full_name, issue in matches
        if issue.get("state") == "open" and not issue.get("pull_request")
    )
    print(
        f"issue_number={number} repos_checked={checked} "
        f"matches={len(matches)} open_issue_matches={open_issue_matches}"
    )
    for full_name, issue in matches:
        kind = "pull_request" if issue.get("pull_request") else "issue"
        labels = ",".join(l.get("name", "") for l in issue.get("labels") or [] if l.get("name"))
        print(
            f"{full_name} #{issue.get('number')} [{kind}] state={issue.get('state')} "
            f"title={issue.get('title')} url={issue.get('html_url')} "
            f"created_at={issue.get('created_at')} labels={labels or '-'}"
        )
        body = (issue.get("body") or "").strip()
        if body:
            print("body_preview=" + body.replace("\n", " ")[:500])
    return 0


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except Exception:
        return datetime.fromtimestamp(0, timezone.utc)


def open_summary(limit: int = 30) -> int:
    base, token = _client()
    total = 0
    newest = []
    repos_checked = 0
    for full_name in iter_repos(base, token):
        repos_checked += 1
        repo_count = 0
        page = 1
        while True:
            issues = _get_json(
                base,
                token,
                f"/repos/{_repo_path(full_name)}/issues?state=open&type=issues&limit=50&page={page}",
            )
            if not isinstance(issues, list) or not issues:
                break
            repo_count += len(issues)
            for issue in issues:
                newest.append((full_name, issue))
            if len(issues) < 50:
                break
            page += 1
        total += repo_count

    newest.sort(key=lambda item: _parse_time(item[1].get("created_at", "")), reverse=True)
    print(f"total_open_issues={total} repos_checked={repos_checked}")
    for full_name, issue in newest[:limit]:
        print(f"{full_name} #{issue.get('number')} {issue.get('created_at')} {issue.get('title')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv or argv[0] in {"-h", "--help"}:
        print("usage: python -m jarvis.gitea_tools issue <number> | open-summary [limit]")
        return 0
    command = argv.pop(0)
    if command == "issue" and argv:
        return issue_number(int(argv[0]))
    if command == "open-summary":
        limit = int(argv[0]) if argv else 30
        return open_summary(limit)
    raise SystemExit(f"unknown gitea_tools command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
