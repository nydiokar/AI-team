"""[A85] Static, docker-free acceptance invariants for the worker container.

These tests parse the Dockerfile / compose files / harness as text and assert
the structural guarantees the executable acceptance gate depends on. They run
ANYWHERE (no docker, no build) and exist so a regression in the container
boundary is caught in ordinary CI, not only on a docker-capable host.

The EXECUTABLE proofs (image builds, runs non-root, mounts writable,
CODEX_HOME persists, adapters run in-image) live in
scripts/container_acceptance.sh and are DEFERRED to a docker env — see
docs/WORKER_CONTAINER_ACCEPTANCE.md. Nothing here claims those were run.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "Dockerfile"
COMPOSE_WORKER = REPO / "deploy" / "compose.worker.yaml"
ENTRYPOINT = REPO / "deploy" / "docker-entrypoint.sh"
HARNESS = REPO / "scripts" / "container_acceptance.sh"
PYPROJECT = REPO / "pyproject.toml"
CONSTRAINTS = REPO / "constraints.txt"


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text()


@pytest.fixture(scope="module")
def compose_worker() -> str:
    return COMPOSE_WORKER.read_text()


# --------------------------------------------------------------------------
# Non-root execution.
# --------------------------------------------------------------------------
def test_image_creates_dedicated_nonroot_user(dockerfile: str) -> None:
    """A non-root ai-team account (uid 10001) must be created in the image."""
    assert re.search(r"useradd\s+--uid\s+10001", dockerfile), (
        "expected a non-root ai-team user (uid 10001) to be created"
    )


def test_entrypoint_never_execs_as_root() -> None:
    """The entrypoint must drop privileges and must refuse to run as uid 0."""
    text = ENTRYPOINT.read_text()
    assert "setpriv" in text, "entrypoint must drop privileges via setpriv"
    assert "--reuid" in text and "--regid" in text
    # Root is explicitly rewritten to the ai-team fallback uid/gid.
    assert re.search(r'\[\s*"\$uid"\s*=\s*"0"\s*\]\s*&&\s*uid=10001', text), (
        "entrypoint must never leave the process running as uid 0"
    )
    assert re.search(r'\[\s*"\$gid"\s*=\s*"0"\s*\]\s*&&\s*gid=10001', text)


def test_compose_worker_cannot_regain_privileges(compose_worker: str) -> None:
    assert "no-new-privileges:true" in compose_worker
    # cap_drop ALL, only SETUID/SETGID re-added for the one-shot privilege drop.
    assert re.search(r"cap_drop:\s*\n\s*- ALL", compose_worker)
    caps = re.findall(r"- (SETUID|SETGID|[A-Z_]+)", compose_worker)
    assert set(caps) <= {"ALL", "SETUID", "SETGID"}, f"unexpected capability: {caps}"


# --------------------------------------------------------------------------
# Codex pinned via a Renovate-readable Docker build ARG (not a literal RUN pin).
# --------------------------------------------------------------------------
def test_codex_pinned_as_renovate_readable_arg(dockerfile: str) -> None:
    """Codex must be pinned as an ARG with a Renovate datasource annotation,
    and the RUN line must consume the ARG (not a hardcoded literal)."""
    assert re.search(
        r"#\s*renovate:\s*datasource=npm\s+depName=@openai/codex\s*\n"
        r"ARG\s+CODEX_VERSION=\d+\.\d+\.\d+",
        dockerfile,
    ), "CODEX_VERSION must be a Renovate-annotated build ARG"
    assert "@openai/codex@${CODEX_VERSION}" in dockerfile, (
        "the npm install must consume the CODEX_VERSION ARG"
    )


def test_claude_code_pinned_as_renovate_readable_arg(dockerfile: str) -> None:
    assert re.search(
        r"#\s*renovate:\s*datasource=npm\s+depName=@anthropic-ai/claude-code\s*\n"
        r"ARG\s+CLAUDE_CODE_VERSION=\d+\.\d+\.\d+",
        dockerfile,
    ), "CLAUDE_CODE_VERSION must be a Renovate-annotated build ARG"
    assert "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" in dockerfile


def test_no_hardcoded_agent_version_literal_in_run(dockerfile: str) -> None:
    """Guard against reintroducing a literal `<pkg>@1.2.3` in a RUN line, which
    would break Renovate tracking and the requested-vs-actual assertion."""
    assert not re.search(r"@openai/codex@\d+\.\d+\.\d+", dockerfile), (
        "codex version must be an ARG, not a hardcoded literal"
    )
    assert not re.search(r"@anthropic-ai/claude-code@\d+\.\d+\.\d+", dockerfile)


def test_requested_versions_recorded_in_env(dockerfile: str) -> None:
    """The image must record requested versions in ENV so a running container
    can report requested-vs-actual without the build context."""
    assert "AI_TEAM_REQUESTED_CODEX_VERSION=${CODEX_VERSION}" in dockerfile
    assert "AI_TEAM_REQUESTED_CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION}" in dockerfile


# --------------------------------------------------------------------------
# Single dependency authority: pyproject + constraints. No second authority,
# no runtime pip-install of a runtime into a running container.
# --------------------------------------------------------------------------
def test_python_deps_come_from_pyproject_with_constraints(dockerfile: str) -> None:
    assert re.search(r"pip install -c constraints\.txt \.", dockerfile), (
        "python deps must install from pyproject via `pip install -c constraints.txt .`"
    )


def test_no_second_dependency_authority_file() -> None:
    """No requirements.txt masquerading as a parallel dependency authority."""
    assert not (REPO / "requirements.txt").exists(), (
        "requirements.txt would be a second dependency authority; use pyproject/constraints"
    )


def test_no_runtime_pip_install_of_a_runtime(dockerfile: str) -> None:
    """Runtimes (node agents, codex, claude) must be baked at build time, never
    pip/npm-installed into a *running* container. All install RUNs live in the
    build stages above the ENTRYPOINT/CMD, never in the entrypoint script."""
    entry = ENTRYPOINT.read_text()
    assert "pip install" not in entry and "npm install" not in entry, (
        "the entrypoint must not install anything at container start"
    )


def test_pyproject_and_constraints_present() -> None:
    assert PYPROJECT.exists() and CONSTRAINTS.exists()
    # constraints pins the exact stack the prod venv runs.
    assert "claude-agent-sdk==" in CONSTRAINTS.read_text()


# --------------------------------------------------------------------------
# Dedicated CODEX_HOME volume declared and pointed at the persistent mount.
# --------------------------------------------------------------------------
def test_codex_home_is_dedicated_persistent_volume(compose_worker: str) -> None:
    assert "CODEX_HOME: /app/.codex" in compose_worker, "CODEX_HOME must be set"
    # A dedicated bind volume backs /app/.codex so auth survives recreation.
    assert re.search(r"/workers/\$\{WORKER_NODE_ID\}/codex:/app/\.codex", compose_worker), (
        "a dedicated persistent volume must back CODEX_HOME (/app/.codex)"
    )


def test_claude_auth_dir_is_persistent_volume(compose_worker: str) -> None:
    assert re.search(r"/workers/\$\{WORKER_NODE_ID\}/claude:/app/\.claude", compose_worker), (
        "a dedicated persistent volume must back Claude auth (/app/.claude)"
    )


def test_project_root_mounted_read_write(compose_worker: str) -> None:
    assert re.search(
        r"\$\{WORKER_PROJECTS_ROOT[^}]*\}:\$\{WORKER_PROJECTS_ROOT\}:rw", compose_worker
    ), "the worker project root must be bind-mounted read-write"


# --------------------------------------------------------------------------
# Harness honesty: fails loudly without docker, never fakes success.
# --------------------------------------------------------------------------
def test_harness_skips_loudly_without_docker() -> None:
    text = HARNESS.read_text()
    assert "SKIPPED: docker unavailable (deferred gate)" in text, (
        "harness must announce a docker-absent skip"
    )
    # The skip path must exit NON-ZERO (2) so a skipped gate is never a pass.
    assert re.search(r"docker unavailable.*\n(?:.*\n){0,6}?\s*exit 2", text), (
        "the docker-absent path must exit non-zero"
    )


def test_harness_asserts_requested_vs_actual_codex() -> None:
    text = HARNESS.read_text()
    assert "codex version drift" in text, (
        "harness must fail on requested-vs-actual codex drift"
    )


def test_harness_proves_nonroot_and_persistence() -> None:
    text = HARNESS.read_text()
    assert "ran as root" in text, "harness must assert non-root execution"
    assert "did not persist across recreation" in text, (
        "harness must assert CODEX_HOME volume persistence across recreation"
    )
