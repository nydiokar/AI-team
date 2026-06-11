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
#   2. Refuse to redeploy a commit we already deployed + failed (poison guard).
#   3. Record current commit (for rollback), fast-forward to origin/main.
#   4. Restart the affected PM2 processes (--update-env).
#   5. Health-gate: the reloaded PM2 processes must come back `online` AND stay
#      up (not crash-loop); additionally the task-server HTTP /health must answer
#      if a URL is configured.
#   6. On health failure -> roll back to the previous commit, restart again,
#      record the bad SHA so we don't redeploy it, and exit non-zero (loud).
#      A bad commit never leaves the gateway down silently, and never causes an
#      every-2-minute redeploy/rollback loop.
#
# NOTE on "reload": all ai-team PM2 apps run in fork mode, where `pm2 reload`
# is NOT zero-downtime — it is equivalent to a restart. We use `restart` plainly
# so behaviour is honest; expect a brief gateway blip during deploy.
#
# SAFETY:
#   - Only ever deploys `main`. Never touches any other ref.
#   - A lock file prevents overlapping runs.
#   - A poison-commit file stops a known-bad SHA from being redeployed in a loop.
#   - AI_TEAM_TEST_MODE is exported so nothing this script triggers can invoke a
#     paid backend CLI (test cost guard posture).
#
# CONFIG (env, with safe defaults):
#   DEPLOY_REPO_DIR        repo root            (default: script's repo)
#   DEPLOY_BRANCH          branch to track      (default: main)
#   DEPLOY_PM2_APPS        space-sep PM2 names   (default: "ai-team-gateway")
#   DEPLOY_HEALTH_URL      task-server health    (default: http://127.0.0.1:9002/health;
#                          set empty to rely on PM2 process status alone)
#   DEPLOY_HEALTH_TIMEOUT  seconds to wait      (default: 60)
#   DEPLOY_STABLE_SECS     secs a restarted app must stay up (default: 8)
#   DEPLOY_LOCK            lock file path       (default: <repo>/.deploy.lock)
#   DEPLOY_POISON          failed-SHA marker    (default: <repo>/.deploy.poison)

set -uo pipefail

REPO_DIR="${DEPLOY_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BRANCH="${DEPLOY_BRANCH:-main}"
# Default to BOTH gateway and the standalone task server: in the live split
# deployment MESH_EMBEDDED_SERVER=false, so ai-team-server (not the gateway)
# owns the 9002 /health port. Reloading both keeps shared src/ code consistent
# and makes the 9002 health check meaningful. Override for a gateway-only or
# embedded host. Any app in this list that PM2 doesn't know about on this host
# is skipped (see known_apps), so the same default is safe on an embedded-only
# host where ai-team-server isn't defined.
PM2_APPS="${DEPLOY_PM2_APPS:-ai-team-gateway ai-team-server}"
HEALTH_URL="${DEPLOY_HEALTH_URL-http://127.0.0.1:9002/health}"
HEALTH_TIMEOUT="${DEPLOY_HEALTH_TIMEOUT:-60}"
STABLE_SECS="${DEPLOY_STABLE_SECS:-8}"
LOCK_FILE="${DEPLOY_LOCK:-$REPO_DIR/.deploy.lock}"
POISON_FILE="${DEPLOY_POISON:-$REPO_DIR/.deploy.poison}"

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

# --- poison guard: don't redeploy a commit we already deployed + failed -----
# Without this, a bad commit on origin/main would be re-fetched, re-deployed,
# fail health, and roll back every 2 minutes forever (log spam + gateway blips)
# until a human pushes a fix.
if [ -f "$POISON_FILE" ] && [ "$(cat "$POISON_FILE" 2>/dev/null)" = "$remote_sha" ]; then
  log "refusing to redeploy ${remote_sha:0:8}: it previously failed the health gate (see $POISON_FILE). Push a new commit to clear."
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

# pm2_status APP -> prints the process status (online/errored/stopped/...) or
# "missing" if PM2 doesn't know the app. Uses jlist (JSON) so we don't parse the
# decorated table. Falls back to a grep on plain output if jq is absent.
pm2_status() {
  # Parse PM2's JSON (jlist) for the app's status. python3 is guaranteed on the
  # Pi5 (it runs the gateway); prefer jq if present, else python3.
  local app="$1" out
  out="$(pm2 jlist 2>/dev/null)" || { echo "unknown"; return; }
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$out" | jq -r --arg a "$app" '(.[] | select(.name==$a) | .pm2_env.status) // "missing"' | head -n1
  else
    printf '%s' "$out" | python3 -c "import sys,json
d=json.load(sys.stdin)
print(next((p['pm2_env']['status'] for p in d if p.get('name')=='$app'),'missing'))" 2>/dev/null || echo "unknown"
  fi
}

# pm2_restarts APP -> PM2's cumulative restart counter for the app (or 0).
# A climbing counter during the stability window means a crash-loop.
pm2_restarts() {
  local app="$1" out
  out="$(pm2 jlist 2>/dev/null)" || { echo "0"; return; }
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$out" | jq -r --arg a "$app" '(.[] | select(.name==$a) | .pm2_env.restart_time) // 0' | head -n1
  else
    printf '%s' "$out" | python3 -c "import sys,json
d=json.load(sys.stdin)
print(next((p['pm2_env'].get('restart_time',0) for p in d if p.get('name')=='$app'),0))" 2>/dev/null || echo "0"
  fi
}

# health_ok: every reloaded app must be `online` AND stay online for
# STABLE_SECS (catches a crash-loop where PM2 keeps restarting a broken
# process). Then, if a health URL is configured, it must answer ok.
health_ok() {
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  # snapshot restart counters so we can detect a crash-loop (counter climbing)
  local app rc_before
  declare -A RC0=()
  for app in $ACTIVE_APPS; do RC0[$app]="$(pm2_restarts "$app")"; done
  # 1) wait for every app to reach `online`
  for app in $ACTIVE_APPS; do
    while :; do
      local st; st="$(pm2_status "$app")"
      [ "$st" = "online" ] && break
      if [ "$(date +%s)" -ge "$deadline" ]; then
        log "health: $app not online (status=$st) within ${HEALTH_TIMEOUT}s"
        return 1
      fi
      sleep 2
    done
  done
  # 2) it must STAY online (no crash-loop) for STABLE_SECS — verify both the
  #    status AND that PM2's restart counter did not climb.
  sleep "$STABLE_SECS"
  for app in $ACTIVE_APPS; do
    local st; st="$(pm2_status "$app")"
    if [ "$st" != "online" ]; then
      log "health: $app did not stay online (status=$st after ${STABLE_SECS}s) — likely crash-looping"
      return 1
    fi
    local rc_now; rc_now="$(pm2_restarts "$app")"
    if [ "${rc_now:-0}" -gt "${RC0[$app]:-0}" ]; then
      log "health: $app restarted again during the stability window (restart_time ${RC0[$app]} -> $rc_now) — crash-looping on the new code"
      return 1
    fi
  done
  # 3) optional HTTP check (task server on :9002). Skip if no URL is configured,
  #    OR if the default :9002 URL is in use but no process that serves it
  #    (ai-team-server, or the gateway with the server embedded) is deployed on
  #    this host — otherwise we'd roll back a healthy gateway over a port nobody
  #    is listening on. An explicitly-overridden URL is always honoured.
  local run_http=0
  if [ -n "$HEALTH_URL" ]; then
    if [ "$HEALTH_URL" != "http://127.0.0.1:9002/health" ]; then
      run_http=1   # explicit override — always check
    elif printf '%s\n' $ACTIVE_APPS | grep -qx "ai-team-server"; then
      run_http=1   # standalone server present -> :9002 is meaningful
    else
      log "health: skipping HTTP $HEALTH_URL (no ai-team-server on this host; relying on PM2 status)"
    fi
  fi
  if [ "$run_http" -eq 1 ]; then
    local hdeadline=$(( $(date +%s) + 20 ))
    while [ "$(date +%s)" -lt "$hdeadline" ]; do
      if curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
        return 0
      fi
      sleep 3
    done
    log "health: HTTP $HEALTH_URL did not report ok (task server down or wrong port?)"
    return 1
  fi
  return 0
}

# Apps from PM2_APPS that PM2 actually knows about on THIS host. Lets the same
# default (gateway + server) work on an embedded-only host where ai-team-server
# isn't defined — unknown apps are skipped, not waited on.
known_apps() {
  local app present
  for app in $PM2_APPS; do
    present="$(pm2_status "$app")"
    [ "$present" != "missing" ] && [ "$present" != "unknown" ] && echo "$app"
  done
}

restart_apps() {
  # fork-mode apps: pm2 reload == restart (no zero-downtime). Use restart plainly.
  local app
  for app in $ACTIVE_APPS; do
    log "pm2 restart $app"
    pm2 restart "$app" --update-env >/dev/null 2>&1
  done
}

ACTIVE_APPS="$(known_apps)"
if [ -z "$ACTIVE_APPS" ]; then
  log "FATAL: none of [$PM2_APPS] are running under PM2 on this host; nothing to deploy to"
  exit 1
fi
log "target apps on this host: $(echo "$ACTIVE_APPS" | tr '\n' ' ')"

restart_apps

if health_ok; then
  log "deploy OK: ${remote_sha:0:8} healthy"
  rm -f "$POISON_FILE" 2>/dev/null   # clear any stale poison marker
  exit 0
fi

# --- health gate failed -> roll back --------------------------------------
log "HEALTH CHECK FAILED after deploying ${remote_sha:0:8}; rolling back to ${local_sha:0:8}"
# Record the bad SHA so the next cron run won't redeploy it in a loop.
echo "$remote_sha" > "$POISON_FILE" 2>/dev/null
git reset --hard "$local_sha" >/dev/null 2>&1
restart_apps
if health_ok; then
  log "rollback to ${local_sha:0:8} healthy; bad commit ${remote_sha:0:8} quarantined in $POISON_FILE (push a fix to clear)"
else
  log "CRITICAL: rollback also failed health check; gateway may be DOWN — manual intervention required"
fi
exit 1
