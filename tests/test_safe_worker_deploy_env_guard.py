"""[A73/P1-4] Deploy-time .env file-mode guard.

Proves safe_worker_deploy refuses to load an env file whose POSIX mode exposes it
to group/other, unless the operator explicitly overrides with
AI_TEAM_ALLOW_LOOSE_ENV=1. The guard is a pure function — no dotenv load, no pm2.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "safe_worker_deploy",
    Path(__file__).resolve().parent.parent / "scripts" / "safe_worker_deploy.py")
safe_worker_deploy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(safe_worker_deploy)


@pytest.fixture
def loose_env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text("WORKER_TOKEN=x\n")
    p.chmod(0o644)
    return str(p)


@pytest.fixture
def private_env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text("WORKER_TOKEN=x\n")
    p.chmod(0o600)
    return str(p)


def test_private_mode_passes(private_env_file):
    safe_worker_deploy._assert_env_file_private(private_env_file)  # must not raise


def test_loose_mode_raises(loose_env_file):
    with pytest.raises(RuntimeError, match="group/other"):
        safe_worker_deploy._assert_env_file_private(loose_env_file)


def test_loose_mode_override_allows(loose_env_file, monkeypatch):
    monkeypatch.setenv("AI_TEAM_ALLOW_LOOSE_ENV", "1")
    safe_worker_deploy._assert_env_file_private(loose_env_file)  # must not raise


def test_missing_file_passes(tmp_path):
    safe_worker_deploy._assert_env_file_private(str(tmp_path / "does-not-exist.env"))


def test_override_not_read_from_env_file(loose_env_file, monkeypatch, tmp_path):
    """The override must come from the operator's process env, never from the very
    file being guarded — a loose .env containing AI_TEAM_ALLOW_LOOSE_ENV must not
    disarm the guard."""
    monkeypatch.delenv("AI_TEAM_ALLOW_LOOSE_ENV", raising=False)
    with pytest.raises(RuntimeError):
        safe_worker_deploy._assert_env_file_private(loose_env_file)
