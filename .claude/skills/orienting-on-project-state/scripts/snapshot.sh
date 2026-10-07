#!/usr/bin/env bash
# One bounded, read-only orientation snapshot: priorities, job queue, git, PRs, live state.
# Every section fails soft so one broken probe never hides the rest.
set -u
REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
cd "$REPO" || exit 1
ROW_CHARS=240   # CONTEXT table rows run to 2k+ chars; the head of a row carries job/status/what

echo "== now: $(date -u +%Y-%m-%dT%H:%MZ)  branch=$(git branch --show-current)"

echo "== CONTEXT.md — Current Focus + Active Work (rows truncated to $ROW_CHARS chars)"
awk '/^## Current Focus/{p=1} /^## Recent shift notes/{p=0} p' .ai/CONTEXT.md \
  | grep -vE '^\s*$|^---$' | cut -c1-"$ROW_CHARS" | head -45

echo "== dispatch queue (dispatch_state.py --audit)"
.venv/bin/python scripts/dispatch/dispatch_state.py --audit 2>&1 | head -45 || echo "audit failed"

echo "== DISPATCH_LOG newest rows"
grep -E '^\| A[0-9]+' .ai/dispatch/DISPATCH_LOG.md | head -5 | cut -c1-"$ROW_CHARS"

echo "== git"
git fetch -q origin 2>/dev/null || echo "(fetch failed — origin state may be stale)"
git status --short | grep -v '^?? .worktrees/' | head -20
echo "-- origin/main (last 8)"; git log --oneline -8 origin/main
echo "-- local branches not merged into origin/main"; git branch --no-merged origin/main --format='%(refname:short) %(committerdate:short)' | head -10
echo "-- worktrees"; git worktree list | head -10

echo "== open PRs"
gh pr list --state open --limit 10 --json number,title,headRefName,updatedAt \
  -q '.[] | "#\(.number) \(.headRefName) (\(.updatedAt[:10])) \(.title)"' 2>/dev/null || echo "gh unavailable"

echo "== live"
"$REPO/.claude/skills/checking-live-state/scripts/live_status.sh" 2>&1 | grep -vE '^\{"status":"ok"' | head -20
