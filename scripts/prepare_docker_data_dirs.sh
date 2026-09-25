#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: sudo APP_UID=<uid> APP_GID=<gid> $0 /absolute/data/root [worker-node-id ...]" >&2
  exit 2
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root so the bind mounts can be owned by the container runtime UID." >&2
  exit 1
fi

data_root=$1
shift
case "$data_root" in
  /*) ;;
  *) echo "Data root must be an absolute path." >&2; exit 2 ;;
esac

# Must match APP_UID/APP_GID in the repo-root .env (the host owner of the
# bind-mounted repositories). Defaults to the invoking sudo user.
uid=${APP_UID:-${SUDO_UID:?set APP_UID or run via sudo}}
gid=${APP_GID:-${SUDO_GID:?set APP_GID or run via sudo}}

install -d -m 0700 -o "$uid" -g "$gid" "$data_root" "$data_root/controller" "$data_root/workers"
for directory in state logs tasks results summaries; do
  install -d -m 0700 -o "$uid" -g "$gid" "$data_root/controller/$directory"
done
for node_id in "$@"; do
  install -d -m 0700 -o "$uid" -g "$gid" "$data_root/workers/$node_id"
  for directory in state logs tasks results summaries codex claude; do
    install -d -m 0700 -o "$uid" -g "$gid" "$data_root/workers/$node_id/$directory"
  done
done
