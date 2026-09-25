#!/usr/bin/env sh
set -eu

export HOME=/app
export CODEX_HOME=/app/.codex

exec setpriv \
  --reuid="${APP_UID:-10001}" \
  --regid="${APP_GID:-10001}" \
  --clear-groups \
  "$@"
