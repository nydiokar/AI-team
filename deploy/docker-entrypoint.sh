#!/usr/bin/env sh
set -eu

export HOME=/app
export CODEX_HOME=/app/.codex

if [ -f /run/secrets/role.env ]; then
  cp /run/secrets/role.env /tmp/role.env
  chmod 600 /tmp/role.env
  chown 10001:10001 /tmp/role.env
  export AI_TEAM_ENV_FILE=/tmp/role.env
fi

exec setpriv --reuid=10001 --regid=10001 --clear-groups "$@"
