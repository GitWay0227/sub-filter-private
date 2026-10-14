#!/usr/bin/env bash
# L1 watchdog: if the artifacts-branch pool goes stale (GitHub cron skipped),
# dispatch a scan run manually. Runs from a local scheduler. Expects a local
# SOCKS proxy on 127.0.0.1:1081 for API access (operator-provided, see README).
set -u
REPO_URL="https://github.com/GitWay0227/sub-filter-private.git"
CLONE="$HOME/.cache/sub-filter-watchdog/clone"
MAX_AGE=$((2 * 3600))
LOG="$HOME/.local/state/sub-filter-watchdog.log"
mkdir -p "$(dirname "$LOG")" "$(dirname "$CLONE")"

log() { echo "$(date -Is) $*" >> "$LOG"; }

# Refresh the artifacts branch (direct https).
if [ ! -d "$CLONE/.git" ]; then
  git clone -q "$REPO_URL" "$CLONE" 2>/dev/null || { log "clone failed"; exit 0; }
fi
git -C "$CLONE" fetch -q origin artifacts 2>/dev/null || { log "fetch failed"; exit 0; }

hdr=$(git -C "$CLONE" show origin/artifacts:pool.yaml 2>/dev/null | sed -n '2p')
ts=$(echo "$hdr" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8} UTC')
[ -n "$ts" ] || { log "no timestamp in pool header"; exit 0; }
age=$(( $(date +%s) - $(date -d "$ts" +%s) ))

if [ "$age" -le "$MAX_AGE" ]; then
  exit 0
fi
log "pool stale: ${age}s old, dispatching scan"

TOK=$(gh auth token 2>/dev/null)
[ -n "$TOK" ] || { log "no gh token"; exit 0; }
for i in 1 2 3; do
  code=$(curl -s -m 20 --socks5-hostname 127.0.0.1:1081 -o /dev/null -w "%{http_code}" \
    -X POST -H "Authorization: Bearer $TOK" -H "Accept: application/vnd.github+json" \
    -d '{"ref":"main"}' \
    "https://api.github.com/repos/GitWay0227/sub-filter-private/actions/workflows/scan.yml/dispatches")
  if [ "$code" = "204" ]; then log "dispatch ok (attempt $i)"; exit 0; fi
  sleep 20
done
log "dispatch failed after 3 attempts (last code: $code)"
