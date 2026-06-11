#!/usr/bin/env bash
#
# T1 — Pull-based auto-deploy for the gateway/server host (the Pi5 `kanebra`).
#
# WHY pull-based (not GitHub Actions -> SSH): the Pi5 is a home server behind
# NAT; the user does not want CI reaching into the tailnet or inbound SSH.
# Instead this runs *on* the Pi5, polls origin/main, and self-deploys. Driven by
# PM2 as a cron-restart process (see ecosystem.config.js `ai-team-deploy`), so
# PM2 owns scheduling/restart/logs — no systemd timer needed.
#
# SCOPE: gateway/server host ONLY. Worker boxes (e.g. Horse) do NOT auto-deploy
# from here — auto-restarting a worker mid-task drops its in-flight claim and
# costs the gateway a full dispatch timeout (the T4 bug). Workers update on their
# own cadence. See .ai/NEXT_TASKS.md T1/T4.
#
# WHAT IT DOES, each invocation:
#   1. git fetch; if origin/main == local HEAD -> nothing to do, exit 0.
#   2. Record current commit (for rollback), fast-forward to origin/main.
#   3. `pm2 reload` the affected processes (reload, not restart -> less downtime).
#   4. Health-gate: poll the gateway HTTP /health until ok or timeout.
#   5. On health failure -> roll back to the previous commit, reload again, and
#      exit non-zero (loud). A bad commit never leaves the gateway down silently.
#
# SAFETY:
#   - Only ever deploys `main`. Never touches any other ref.
#   - A lock file prevents overlapping runs.
#   - AI_TEAM_TEST_MODE is exported so nothing this script triggers can invoke a
#     paid backend CLI (test cost guard posture).
#
# CONFIG (env, with safe defaults):
#   DEPLOY_REPO_DIR        repo root            (default: script's repo)
#   DEPLOY_BRANCH          branch to track      (default: main)
#   DEPLOY_PM2_APPS        space-sep PM2 names   (default: "ai-team-gateway")
#   DEPLOY_HEALTH_URL      health endpoint      (default: http://127.0.0.1:9002/health)
#   DEPLOY_HEALTH_TOKEN    bearer for /health   (default: $WORKER_TOKEN, may be empty)
#   DEPLOY_HEALTH_TIMEOUT  seconds to wait      (default: 60)
#   DEPLOY_LOCK            lock file path       (default: <repo>/.deploy.lock)

set -uo pipefail

REPO_DIR="${DEPLOY_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BRANCH="${DEPLOY_BRANCH:-main}"
PM2_APPS="${DEPLOY_PM2_APPS:-ai-team-gateway}"
HEALTH_URL="${DEPLOY_HEALTH_URL:-http://127.0.0.1:9002/health}"
HEALTH_TOKEN="${DEPLOY_HEALTH_TOKEN:-${WORKER_TOKEN:-}}"
HEALTH_TIMEOUT="${DEPLOY_HEALTH_TIMEOUT:-60}"
LOCK_FILE="${DEPLOY_LOCK:-$REPO_DIR/.deploy.lock}"

# Never let anything spawned from here touch a paid backend (test cost guard).
export AI_TEAM_TEST_MODE=1

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [auto_deploy] $*"; }

# --- single-flight lock (no overlapping deploys) --------------------------
exec 9>"$LOCK_FILE" || { log "FATAL: cannot open lock $LOCK_FILE"; exit 1; }
if ! flock -n 9; then
  log "another deploy is in progress; skipping this run"
  exit 0
fi

cd "$REPO_DIR" || { log "FATAL: repo dir $REPO_DIR missing"; exit 1; }

# --- only ever operate on the tracked branch ------------------------------
current_branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$current_branch" != "$BRANCH" ]; then
  log "refusing to deploy: on branch '$current_branch', expected '$BRANCH'"
  exit 0
fi

if ! git fetch --quiet origin "$BRANCH"; then
  log "git fetch failed; will retry next run"
  exit 0
fi

local_sha="$(git rev-parse HEAD)"
remote_sha="$(git rev-parse "origin/$BRANCH")"

if [ "$local_sha" = "$remote_sha" ]; then
  # up to date — quiet exit so logs aren't spammed every poll
  exit 0
fi

log "new commit on $BRANCH: ${local_sha:0:8} -> ${remote_sha:0:8}; deploying"

# --- detect which apps actually need a reload (skip docs-only pushes) -----
changed="$(git diff --name-only "$local_sha" "$remote_sha")"
if echo "$changed" | grep -qvE '^(docs/|\.ai/|.*\.md$)'; then
  needs_reload=1
else
  needs_reload=0
  log "changes are docs-only; will fast-forward but skip the reload"
fi

# --- fast-forward only (never rewrite/merge) ------------------------------
if ! git merge --ff-only "origin/$BRANCH" >/dev/null 2>&1; then
  log "FATAL: cannot fast-forward (local has diverged from origin/$BRANCH); manual intervention needed"
  exit 1
fi
log "fast-forwarded to ${remote_sha:0:8}"

if [ "$needs_reload" -eq 0 ]; then
  log "deploy complete (no process reload required)"
  exit 0
fi

health_ok() {
  # Returns 0 when /health reports status ok within HEALTH_TIMEOUT.
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  local auth=()
  [ -n "$HEALTH_TOKEN" ] && auth=(-H "Authorization: Bearer $HEALTH_TOKEN")
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -fsS --max-time 5 "${auth[@]}" "$HEALTH_URL" 2>/dev/null | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
      return 0
    fi
    sleep 3
  done
  return 1
}

reload_apps() {
  for app in $PM2_APPS; do
    log "pm2 reload $app"
    pm2 reload "$app" --update-env >/dev/null 2>&1 || pm2 restart "$app" --update-env >/dev/null 2>&1
  done
}

reload_apps

if health_ok; then
  log "deploy OK: ${remote_sha:0:8} healthy"
  exit 0
fi

# --- health gate failed -> roll back --------------------------------------
log "HEALTH CHECK FAILED after deploying ${remote_sha:0:8}; rolling back to ${local_sha:0:8}"
git reset --hard "$local_sha" >/dev/null 2>&1
reload_apps
if health_ok; then
  log "rollback to ${local_sha:0:8} healthy; bad commit ${remote_sha:0:8} NOT deployed"
else
  log "CRITICAL: rollback also failed health check; gateway may be DOWN — manual intervention required"
fi
exit 1
