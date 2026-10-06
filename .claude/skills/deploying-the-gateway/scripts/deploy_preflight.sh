#!/usr/bin/env bash
# Read-only deploy preflight for the AI-Team control plane (gateway + task-server containers).
# Prints GO/STOP findings and the exact commands for this deploy. Changes nothing.
set -u
REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
cd "$REPO" || exit 1
STOP=0
stop() { echo "STOP: $*"; STOP=1; }

echo "== repo"
git fetch -q origin 2>/dev/null || stop "git fetch failed"
BRANCH="$(git branch --show-current)"
HEAD_SHA="$(git rev-parse --short HEAD)"
[ "$BRANCH" = "main" ] || stop "on branch '$BRANCH' — deploy builds from the working tree; switch to main"
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || stop "HEAD $HEAD_SHA != origin/main $(git rev-parse --short origin/main) — git pull --ff-only"
DIRTY="$(git status --porcelain --untracked-files=no)"
[ -z "$DIRTY" ] || stop "tracked changes in the tree would be baked into the image:"$'\n'"$DIRTY"
echo "HEAD @ $HEAD_SHA"

echo "== what is live now"
LOCAL_ID="$(docker image inspect ai-team:local --format '{{.Id}}' 2>/dev/null | cut -c8-19)"
[ -n "$LOCAL_ID" ] || stop "no ai-team:local image — first deploy is an operator task (docs/RUNBOOKS/OPERATIONS_DOCKER.md)"
BUILT="$(docker image inspect ai-team:local --format "{{.Created}}" 2>/dev/null | sed -E "s/\\.[0-9]+//")"
PROD_TAG="$(docker images ai-team --format '{{.ID}} {{.Tag}}' | awk -v id="$LOCAL_ID" '$1==id && $2 ~ /^prod-/{print $2; exit}')"
echo "ai-team:local id=$LOCAL_ID built=$BUILT tag=${PROD_TAG:-none}"
if [ -n "$PROD_TAG" ]; then
  RANGE="${PROD_TAG#prod-}..HEAD"
  echo "changes going live ($RANGE):"; git log --oneline --first-parent "$RANGE" | head -30
else
  echo "no prod-<sha> tag; merges to main since the image was built:"
  git log --merges --oneline --since="$BUILT" HEAD | head -30
fi

echo "== migrations"
LIVE_SCHEMA="$(curl -s --max-time 5 "http://$(docker port ai-team-task-server-1 9002/tcp 2>/dev/null | head -1)/health" \
  | python3 -c 'import json,sys; print((json.load(sys.stdin).get("db") or {}).get("schema_version",""))' 2>/dev/null)"
HEAD_SCHEMA="$(grep -oE '^\s+\(([0-9]+), """' src/control/db.py | grep -oE '[0-9]+' | sort -n | tail -1)"
echo "live schema_version=${LIVE_SCHEMA:-unknown}  HEAD max migration=$HEAD_SCHEMA"
if [ -z "$LIVE_SCHEMA" ]; then
  stop "cannot read live schema_version — task-server unhealthy? investigate before deploying"
elif [ "$HEAD_SCHEMA" -gt "$LIVE_SCHEMA" ]; then
  echo "MIGRATIONS WILL APPLY on startup ($LIVE_SCHEMA -> $HEAD_SCHEMA): DB backup is mandatory, rollback needs the backup"
fi

echo "== compose + host"
docker compose config --quiet 2>/dev/null || stop "docker compose config failed — host env (DOCKER_DATA_ROOT, MESH_TAILSCALE_IP) not resolvable from $REPO"
df -h --output=avail,target /var/lib/docker 2>/dev/null | tail -1 | sed 's/^/docker disk free: /'

echo
if [ "$STOP" -ne 0 ]; then echo "PREFLIGHT: STOP — fix the findings above"; exit 1; fi
STAMP="$(date -u +%Y%m%dT%H%MZ)"
echo "PREFLIGHT: GO. Commands for this deploy:"
echo "  sqlite3 ~/ai-team-data/controller/state/mesh.db \".backup '$HOME/ai-team-data/backups/mesh-pre-$HEAD_SHA-$STAMP.db'\""
echo "  docker tag ai-team:local ai-team:pre-$HEAD_SHA"
echo "  docker compose build gateway"
echo "  docker compose up -d --no-build gateway task-server"
echo "  docker tag ai-team:local ai-team:prod-$HEAD_SHA"
