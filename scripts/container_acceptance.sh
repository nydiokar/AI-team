#!/usr/bin/env bash
# A85 — Containerized worker acceptance harness.
#
# Proves the worker image satisfies the acceptance gate WHEN RUN in a
# docker-capable environment. It is intentionally executable evidence, not a
# unit test: it builds the image, runs a container, and inspects real runtime
# behaviour. The static invariants (USER, ARG pins, CODEX_HOME volume, single
# dependency authority) are covered separately by
# tests/test_container_acceptance.py, which runs anywhere.
#
# HONESTY CONTRACT
#   - If docker/podman is unavailable, this script prints
#       "SKIPPED: docker unavailable (deferred gate)"
#     and exits NON-ZERO (2) when invoked as the acceptance gate, so no caller
#     can mistake a skipped run for a pass. It NEVER emits fake success.
#   - Every proof below runs a real container and asserts on its real output.
#
# USAGE
#   scripts/container_acceptance.sh            # full gate (build + run + prove)
#   scripts/container_acceptance.sh --inventory-only   # just the runtime report
#
# What this does NOT do (SCOPE OUT / A86): no registry push, no production
# switch, no worker restart, no Renovate automation, no node-maintenance API,
# no paid backend calls. Authenticated real-runtime smoke and session resume
# are operator-gated (see docs/WORKER_CONTAINER_ACCEPTANCE.md, RESERVED R2).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${AI_TEAM_WORKER_IMAGE:-ai-team-worker-agents:acceptance}"
TARGET="worker-agents"
CODEX_VOL="ai-team-acceptance-codex"
DOCKER_BIN="${DOCKER_BIN:-}"

log()  { printf '\033[1m[acceptance]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }
pass() { printf '\033[32m[PASS]\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------------------
# Guard: no docker => loud skip, non-zero exit. Never a silent/fake pass.
# ---------------------------------------------------------------------------
resolve_docker() {
  if [ -n "${DOCKER_BIN}" ]; then return 0; fi
  if command -v docker  >/dev/null 2>&1; then DOCKER_BIN="docker";  return 0; fi
  if command -v podman  >/dev/null 2>&1; then DOCKER_BIN="podman";  return 0; fi
  return 1
}

if ! resolve_docker; then
  echo "SKIPPED: docker unavailable (deferred gate)" >&2
  echo "This acceptance gate requires a docker/podman-capable host. It was NOT run." >&2
  echo "Nothing was proven. See docs/WORKER_CONTAINER_ACCEPTANCE.md (Deferred gate)." >&2
  exit 2
fi
log "using container engine: ${DOCKER_BIN}"

INVENTORY_ONLY=0
[ "${1:-}" = "--inventory-only" ] && INVENTORY_ONLY=1

cleanup() {
  "${DOCKER_BIN}" volume rm -f "${CODEX_VOL}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1. Reproducible build from a clean checkout of THIS repo.
# ---------------------------------------------------------------------------
GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"
log "building ${IMAGE} (target=${TARGET}) at git ${GIT_SHA}"
"${DOCKER_BIN}" build \
  --target "${TARGET}" \
  --build-arg "AI_TEAM_GIT_SHA=${GIT_SHA}" \
  -t "${IMAGE}" \
  "${REPO_ROOT}"
pass "image built"

# Helper: run a throwaway container with an override entrypoint (so we test the
# real image contents, not the daemon). --entrypoint bypasses the setpriv drop
# only where we explicitly need root introspection; the non-root proof below
# uses the REAL entrypoint.
run() { "${DOCKER_BIN}" run --rm "$@"; }

# ---------------------------------------------------------------------------
# RUNTIME INVENTORY (always emitted).
# ---------------------------------------------------------------------------
log "runtime inventory"
run --entrypoint sh "${IMAGE}" -c '
  set -e
  echo "image_git_sha=${AI_TEAM_GIT_SHA:-unset}"
  echo "requested_codex=${AI_TEAM_REQUESTED_CODEX_VERSION:-unset}"
  echo "requested_claude_code=${AI_TEAM_REQUESTED_CLAUDE_CODE_VERSION:-unset}"
  echo "actual_codex=$(codex --version 2>/dev/null || echo MISSING)"
  echo "actual_claude_code=$(claude --version 2>/dev/null || echo MISSING)"
  echo "claude_agent_sdk=$(python -c "import claude_agent_sdk as m; print(getattr(m,\"__version__\",\"?\"))" 2>/dev/null || echo MISSING)"
  echo "python=$(python --version 2>&1)"
  echo "node=$(node --version 2>/dev/null || echo MISSING)"
'

# requested-vs-actual Codex assertion (ACCEPTANCE 1).
REQ="$(run --entrypoint sh "${IMAGE}" -c 'printf %s "${AI_TEAM_REQUESTED_CODEX_VERSION:-}"')"
ACT="$(run --entrypoint sh "${IMAGE}" -c 'codex --version 2>/dev/null | grep -oE "[0-9]+\.[0-9]+\.[0-9]+" | head -n1 || true')"
log "codex requested=${REQ} actual=${ACT}"
if [ -z "${ACT}" ]; then
  fail "codex not runnable in image (requested ${REQ})"
elif [ "${REQ}" != "${ACT}" ]; then
  fail "codex version drift: requested ${REQ}, image ships ${ACT}"
else
  pass "codex requested == actual (${ACT})"
fi

if [ "${INVENTORY_ONLY}" = "1" ]; then
  log "inventory-only run complete"
  exit 0
fi

# ---------------------------------------------------------------------------
# 2. Non-root execution via the REAL entrypoint (ACCEPTANCE 2).
#    The entrypoint drops to the owner of /app/state (or ai-team 10001).
# ---------------------------------------------------------------------------
log "non-root execution (real entrypoint)"
WHOAMI_UID="$(run "${IMAGE}" sh -c 'id -u')"
if [ "${WHOAMI_UID}" = "0" ]; then
  fail "container ran as root (uid 0) — entrypoint did not drop privileges"
fi
pass "container runs non-root (uid=${WHOAMI_UID})"

# ---------------------------------------------------------------------------
# 3. Declared project mount is writable + git operations work (ACCEPTANCE 2).
#    Emulate a host repo bind-mounted at WORKER_PROJECTS_ROOT.
# ---------------------------------------------------------------------------
log "project mount read/write + git"
PROJ="$(mktemp -d)"
run \
  -v "${PROJ}:/srv/worker-projects:rw" \
  -e HOME=/app -e CODEX_HOME=/app/.codex \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
  --entrypoint sh "${IMAGE}" -c '
    set -e
    cd /srv/worker-projects
    git init -q .
    git config user.email a@b.c && git config user.name t
    echo hello > f.txt
    git add f.txt && git commit -qm probe
    git rev-parse HEAD >/dev/null
  ' || fail "project mount not writable or git failed"
rm -rf "${PROJ}"
pass "project mount writable and git ops succeed"

# ---------------------------------------------------------------------------
# 4. CODEX_HOME dedicated volume PERSISTS across container recreation
#    (ACCEPTANCE 3). Write a marker in one container, destroy it, mount the
#    same volume in a fresh container, prove the marker survived.
# ---------------------------------------------------------------------------
log "CODEX_HOME persistence across recreation"
"${DOCKER_BIN}" volume create "${CODEX_VOL}" >/dev/null
run -v "${CODEX_VOL}:/app/.codex" -e CODEX_HOME=/app/.codex \
  --entrypoint sh "${IMAGE}" -c 'echo persisted-token > /app/.codex/auth.marker'
MARKER="$(run -v "${CODEX_VOL}:/app/.codex" -e CODEX_HOME=/app/.codex \
  --entrypoint sh "${IMAGE}" -c 'cat /app/.codex/auth.marker 2>/dev/null || echo MISSING')"
if [ "${MARKER}" != "persisted-token" ]; then
  fail "CODEX_HOME volume did not persist across recreation (got: ${MARKER})"
fi
pass "CODEX_HOME volume persists across container recreation"

# ---------------------------------------------------------------------------
# 5. In-image adapter smoke (deterministic, NO paid calls) (ACCEPTANCE 2).
#    - Codex app-server: assert the binary can launch its protocol server.
#      `initialize`/`model/list` against a real provider is operator-gated
#      (needs auth); here we only prove the app-server subcommand exists and
#      the Python client module imports.
#    - Claude SDK: import + confirm transport class is importable (no network).
# ---------------------------------------------------------------------------
log "in-image adapter smoke (deterministic, no paid calls)"
run --entrypoint sh "${IMAGE}" -c '
  set -e
  # Codex app-server subcommand must exist (protocol entrypoint the backend uses).
  codex app-server --help >/dev/null 2>&1 || { echo "codex app-server missing"; exit 1; }
  # Python-side adapters import inside the image.
  python -c "import src.backends.codex_app_server as c; assert hasattr(c, \"CodexAppServerClient\")"
  python -c "import claude_agent_sdk as s; assert s.__version__"
  python -c "from src.worker.agent import WorkerAgent" 2>/dev/null || \
    python -c "import src.worker.agent"
' || fail "in-image adapter smoke failed"
pass "codex app-server present; Claude SDK + worker adapters import in-image"

log "ACCEPTANCE GATE PASSED (deterministic subset)."
log "STILL OPERATOR-GATED (not proven here): authenticated Codex/Claude calls,"
log "session/thread resume across recreation — require real credentials (R2)."
echo "RESULT: PASS (deterministic acceptance subset) at git ${GIT_SHA}"
