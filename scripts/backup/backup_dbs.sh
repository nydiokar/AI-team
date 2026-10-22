#!/usr/bin/env bash
# backup_dbs.sh — verified, rotating snapshots of the controller's operational SQLite DBs.
#
# What: mesh.db (sessions, cases, task/flow/llm events) and quota_windows.db
#   (quota coordinator events + snapshots), read from the controller state dir.
#
# How, per DB: `VACUUM INTO` (one read transaction → a consistent copy even
#   while the gateway writes; never `cp` a live WAL DB) → `PRAGMA integrity_check`
#   must say `ok` → zstd → SHA256SUMS. The run is assembled in .tmp/ and renamed
#   into snapshots/<UTC stamp>/ (same filesystem → atomic), so anything under
#   snapshots/ is complete. A DB that fails is left out of the run and the script
#   exits nonzero (journal tag: aiteam-backup). LATEST names the newest run in
#   which every DB succeeded; retention runs only after such a run.
#
# Retention: newest AI_TEAM_BACKUP_KEEP_RECENT runs, plus the newest run of each
#   of the newest AI_TEAM_BACKUP_KEEP_DAILY days.
#
# Off-host integration point: snapshots/ is append-only immutable directories
#   (sync it as-is, e.g. `rclone copy snapshots/ remote:...`; verify with
#   `sha256sum -c SHA256SUMS` inside a run dir). Nothing here pushes off-host yet.
#
# Restore (operator decision — discards writes after the snapshot): stop the
#   gateway + task-server, move mesh.db{,-wal,-shm} aside, then
#   `zstd -d snapshots/<run>/mesh.db.zst -o <state>/mesh.db`, start again.
#
# Install: crontab line tagged `aiteam-db-backup`; remove with
#   crontab -l | grep -v aiteam-db-backup | crontab -
set -uo pipefail

# cron runs with a minimal PATH.
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"

SRC_DIR="${AI_TEAM_DB_SOURCE_DIR:-$HOME/ai-team-data/controller/state}"
DEST_DIR="${AI_TEAM_BACKUP_DIR:-$HOME/ai-team-backups/db}"
KEEP_RECENT="${AI_TEAM_BACKUP_KEEP_RECENT:-48}"
KEEP_DAILY="${AI_TEAM_BACKUP_KEEP_DAILY:-30}"
DBS=(mesh.db quota_windows.db)

SNAP_DIR="$DEST_DIR/snapshots"
TMP_ROOT="$DEST_DIR/.tmp"
LOG_FILE="$DEST_DIR/backup.log"

log() {
    local msg="$(date -u +%FT%TZ) $*"
    echo "$msg" >>"$LOG_FILE"
    echo "$msg" >&2
}

fail() {
    log "ERROR $*"
    logger -t aiteam-backup -p user.err -- "$*" 2>/dev/null || true
}

mkdir -p "$SNAP_DIR" "$TMP_ROOT" || { echo "cannot create $DEST_DIR" >&2; exit 1; }

exec 9>"$DEST_DIR/.lock"
if ! flock -n 9; then
    fail "previous backup run still holds $DEST_DIR/.lock; skipping"
    exit 75
fi

# Low priority: the gateway shares this disk and CPU.
renice -n 19 $$ >/dev/null 2>&1 || true
ionice -c 3 -p $$ >/dev/null 2>&1 || true

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
work="$TMP_ROOT/$stamp"
rm -rf "$TMP_ROOT"/* && mkdir -p "$work" || { fail "cannot prepare $work"; exit 1; }

failed=0
done_count=0
for db in "${DBS[@]}"; do
    src="$SRC_DIR/$db"
    out="$work/$db"
    if [[ ! -f "$src" ]]; then
        fail "$db: source $src missing"; failed=1; continue
    fi
    if ! sqlite3 -readonly -cmd ".timeout 15000" "$src" "VACUUM INTO '${out//\'/\'\'}'" 2>>"$LOG_FILE"; then
        fail "$db: VACUUM INTO failed"; rm -f "$out"; failed=1; continue
    fi
    check="$(sqlite3 -readonly "$out" "PRAGMA integrity_check" 2>&1)"
    if [[ "$check" != "ok" ]]; then
        fail "$db: integrity_check: ${check:0:200}"; rm -f "$out"; failed=1; continue
    fi
    if ! zstd -q -T0 -3 --rm "$out" -o "$out.zst"; then
        fail "$db: zstd failed"; rm -f "$out" "$out.zst"; failed=1; continue
    fi
    done_count=$((done_count + 1))
done

if (( done_count == 0 )); then
    rm -rf "$work"
    fail "no DB backed up; nothing published"
    exit 1
fi

(cd "$work" && sha256sum -- *.zst >SHA256SUMS) || { fail "checksum failed"; rm -rf "$work"; exit 1; }
mv "$work" "$SNAP_DIR/$stamp" || { fail "publish failed"; exit 1; }

if (( failed )); then
    log "PARTIAL $stamp ($done_count/${#DBS[@]} DBs); LATEST and retention untouched"
    exit 1
fi

echo "$stamp" >"$DEST_DIR/LATEST.tmp" && mv "$DEST_DIR/LATEST.tmp" "$DEST_DIR/LATEST"

# Retention: walk runs newest-first.
declare -A day_seen=()
days=0
i=0
pruned=0
while IFS= read -r run; do
    keep=0
    (( i < KEEP_RECENT )) && keep=1
    day="${run:0:8}"
    if [[ -z "${day_seen[$day]:-}" ]]; then
        day_seen[$day]=1
        days=$((days + 1))
        (( days <= KEEP_DAILY )) && keep=1
    fi
    if (( ! keep )); then
        rm -rf -- "${SNAP_DIR:?}/$run" && pruned=$((pruned + 1))
    fi
    i=$((i + 1))
done < <(find "$SNAP_DIR" -mindepth 1 -maxdepth 1 -type d -name '[0-9]*T*Z' -printf '%f\n' | sort -r)

log "OK $stamp ($(du -sh "$SNAP_DIR/$stamp" | cut -f1)); pruned $pruned"
