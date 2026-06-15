#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIVE_DIR="${JARVIS_LIVE_DIR:-/opt/jarvis}"
LOCK_FILE="${JARVIS_DEPLOY_LOCK:-/tmp/jarvis-deploy.lock}"
SERVICES=(jarvis-dashboard.service jarvis-loop.service)

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

run() {
  echo "+ $*"
  "$@"
}

if [ "$(id -u)" = "0" ]; then
  fail "run Jarvis deploy as yohn, not root"
fi

[ -d "$ROOT/.git" ] || fail "$ROOT is not a git checkout"
[ -d "$LIVE_DIR" ] || fail "$LIVE_DIR does not exist"

exec 9>"$LOCK_FILE"
flock -n 9 || fail "another Jarvis deploy is already running"

branch="$(git -C "$ROOT" branch --show-current)"
[ "$branch" = "main" ] || fail "refusing to deploy branch $branch; merge to main first"

upstream="$(git -C "$ROOT" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
[ "$upstream" = "origin/main" ] || fail "main must track origin/main, not ${upstream:-<none>}"

status="$(git -C "$ROOT" status --porcelain --untracked-files=all)"
[ -z "$status" ] || fail "working tree is dirty; commit intentional changes first"

run git -C "$ROOT" fetch --prune origin
counts="$(git -C "$ROOT" rev-list --left-right --count origin/main...HEAD)"
behind="$(awk '{print $1}' <<<"$counts")"
ahead="$(awk '{print $2}' <<<"$counts")"
[ "${behind:-0}" = "0" ] || fail "main is behind origin/main by $behind commit(s); pull first"
if [ "${ahead:-0}" != "0" ]; then
  run git -C "$ROOT" push origin main
  run git -C "$ROOT" fetch --prune origin
  counts="$(git -C "$ROOT" rev-list --left-right --count origin/main...HEAD)"
  behind="$(awk '{print $1}' <<<"$counts")"
  ahead="$(awk '{print $2}' <<<"$counts")"
  [ "${behind:-0}" = "0" ] && [ "${ahead:-0}" = "0" ] || fail "push did not leave main equal to origin/main"
fi

run python3 -m py_compile "$ROOT"/jarvis/*.py "$ROOT"/jarvis/**/*.py "$ROOT"/skills/*/skill.py

if [ ! -x "$LIVE_DIR/.venv/bin/python" ]; then
  run python3 -m venv "$LIVE_DIR/.venv"
fi
run "$LIVE_DIR/.venv/bin/python" -m pip install -q -r "$ROOT/requirements.txt"

run rsync -a --delete "$ROOT/jarvis/" "$LIVE_DIR/jarvis/"
run rsync -a --delete "$ROOT/skills/" "$LIVE_DIR/skills/"
[ -d "$ROOT/docs" ] && run rsync -a --delete "$ROOT/docs/" "$LIVE_DIR/docs/"
for file in install.sh requirements.txt README.md LICENSE .env.example config.example.yaml docker-compose.yml; do
  [ -f "$ROOT/$file" ] && run install -m 0644 "$ROOT/$file" "$LIVE_DIR/$file"
done
run chmod +x "$LIVE_DIR/install.sh"

commit="$(git -C "$ROOT" rev-parse HEAD)"
short_commit="$(git -C "$ROOT" rev-parse --short HEAD)"
deployed_at="$(date -Is)"
cat >"$LIVE_DIR/.deployed-version.json" <<JSON
{
  "commit": "$commit",
  "short_commit": "$short_commit",
  "branch": "main",
  "repo": "$ROOT",
  "project_key": "jarvis",
  "server_ip": "local",
  "remote_path": "$LIVE_DIR",
  "service": "jarvis-dashboard.service,jarvis-loop.service",
  "tool": "jarvis deploy.sh",
  "deployed_at": "$deployed_at"
}
JSON

run sudo -n systemctl restart "${SERVICES[@]}"
for service in "${SERVICES[@]}"; do
  run systemctl is-active --quiet "$service"
done

echo "Jarvis deployed $short_commit to $LIVE_DIR"
