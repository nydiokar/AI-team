#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "Usage: sudo $0 /absolute/data/root" >&2
  exit 2
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root so the container's non-root UID can own the bind mounts." >&2
  exit 1
fi

data_root=$1
case "$data_root" in
  /*) ;;
  *) echo "Data root must be an absolute path." >&2; exit 2 ;;
esac

install -d -m 0700 -o 10001 -g 10001 "$data_root"
for directory in state logs tasks results summaries codex claude; do
  install -d -m 0700 -o 10001 -g 10001 "$data_root/$directory"
done
