#!/usr/bin/env sh
set -eu

export HOME=/app
export CODEX_HOME=/app/.codex

exec setpriv \
  --reuid=10001 \
  --regid=10001 \
  --clear-groups \
  "$@"
