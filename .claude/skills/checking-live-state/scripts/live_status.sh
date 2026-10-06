#!/usr/bin/env bash
# Read-only snapshot of what is actually running for AI-Team on this host.
# Usage: live_status.sh [FLAG_NAME ...]   (flags are looked up via scripts/ops_flag.sh get)
# Never restarts, writes, or takes a lock. Each probe fails soft and says so.
set -u
REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
CURL=(curl -s --max-time 5)   # 5s: /health answers in ms; longer means the process is wedged

echo "== containers (compose project ai-team)"
docker ps -a --filter label=com.docker.compose.project=ai-team \
  --format '{{.Names}}\t{{.Status}}\t{{.Image}}' 2>/dev/null || echo "docker unavailable"

echo "== image identity"
LOCAL_ID="$(docker image inspect ai-team:local --format '{{.Id}}' 2>/dev/null | cut -c8-19)"
if [ -n "$LOCAL_ID" ]; then
  TAGS="$(docker images ai-team --format '{{.ID}} {{.Tag}}' | awk -v id="$LOCAL_ID" '$1==id && $2!="local"{print $2}' | tr '\n' ' ')"
  CREATED="$(docker image inspect ai-team:local --format '{{.Created}}' | sed -E 's/\.[0-9]+//')"
  echo "ai-team:local id=$LOCAL_ID built=$CREATED aliases=[${TAGS:-none}]"
  PROD_SHA="$(echo "$TAGS" | tr ' ' '\n' | sed -n 's/^prod-//p' | head -1)"
  MAIN_SHA="$(git -C "$REPO" rev-parse --short origin/main 2>/dev/null)"
  if [ -n "$PROD_SHA" ]; then
    BEHIND="$(git -C "$REPO" rev-list --count "$PROD_SHA..origin/main" 2>/dev/null || echo '?')"
    echo "deployed=$PROD_SHA origin/main=$MAIN_SHA commits_not_live=$BEHIND"
  else
    echo "deployed SHA unknown (no prod-<sha> tag on ai-team:local); compare built= time with: git log origin/main --since=<built>"
  fi
fi

echo "== gateway :9003/health"
"${CURL[@]}" http://127.0.0.1:9003/health || echo "UNREACHABLE"
echo

echo "== task-server /health"
TS_ADDR="$(docker port ai-team-task-server-1 9002/tcp 2>/dev/null | head -1)"
if [ -n "$TS_ADDR" ]; then
  "${CURL[@]}" "http://$TS_ADDR/health" | python3 -c 'import json,sys
d=json.load(sys.stdin)
db=d.get("db") or {}
print("status=%s" % d.get("status"), " ".join("%s=%s" % (k, db.get(k)) for k in ("schema_version","nodes_online","nodes_total","tasks_pending","tasks_stale_pending","tasks_claimed","sessions_busy")), "mesh_degraded=%s" % (d.get("mesh_health") or {}).get("degraded"))' 2>/dev/null || echo "UNREACHABLE at $TS_ADDR"
else
  echo "task-server port not published / container missing"
fi

echo "== pm2 (native worker on this host)"
pm2 jlist 2>/dev/null | python3 -c 'import json,sys
for p in json.load(sys.stdin):
    e=p["pm2_env"]; print(p["name"], e["status"], "restarts=%s" % e.get("restart_time"), "cwd=%s" % e.get("pm_cwd"))' 2>/dev/null || echo "pm2 unavailable"

if [ "$#" -gt 0 ]; then
  echo "== flags (running process view via /api/flags)"
  for f in "$@"; do "$REPO/scripts/ops_flag.sh" get "$f" || echo "$f: lookup failed"; done
fi
