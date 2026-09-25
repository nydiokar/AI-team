#!/usr/bin/env sh
set -eu

export HOME=/app
export CODEX_HOME=/app/.codex

# Run as the owner of the bind-mounted state dir (the host data root owner) so
# host repos owned by the same user stay writable. APP_UID/APP_GID override;
# never drop to root, fall back to the image's ai-team user (10001).
uid="${APP_UID:-$(stat -c %u /app/state 2>/dev/null || echo 10001)}"
gid="${APP_GID:-$(stat -c %g /app/state 2>/dev/null || echo 10001)}"
[ "$uid" = "0" ] && uid=10001
[ "$gid" = "0" ] && gid=10001

exec setpriv \
  --reuid="$uid" \
  --regid="$gid" \
  --clear-groups \
  "$@"
