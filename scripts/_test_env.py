"""Shared safety setup for standalone scripts/test_*.py smoke checks."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def configure_test_environment(
    script_name: str,
    *,
    worker_token: str = "script-test-token",
    mesh_enabled: bool = False,
) -> Path:
    """Point project config at an isolated temp DB before importing app modules."""
    temp_dir = Path(tempfile.mkdtemp(prefix=f"ai_team_{script_name}_"))
    db_path = temp_dir / "mesh.db"
    env_path = temp_dir / ".env"
    env_path.write_text(
        "\n".join(
            [
                "AI_TEAM_TEST_MODE=1",
                f"MESH_ENABLED={str(mesh_enabled).lower()}",
                "MESH_SHADOW_WRITE=true",
                f"MESH_DB_PATH={db_path}",
                f"WORKER_TOKEN={worker_token}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    os.environ["AI_TEAM_ENV_FILE"] = str(env_path)
    os.environ["AI_TEAM_TEST_MODE"] = "1"
    os.environ["MESH_ENABLED"] = str(mesh_enabled).lower()
    os.environ["MESH_SHADOW_WRITE"] = "true"
    os.environ["MESH_DB_PATH"] = str(db_path)
    os.environ["WORKER_TOKEN"] = worker_token
    return db_path


def cleanup_test_environment(db_path: Path) -> None:
    """Best-effort cleanup for the temp DB and its environment file."""
    for ext in ("", "-wal", "-shm"):
        p = Path(str(db_path) + ext)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    try:
        shutil.rmtree(db_path.parent)
    except OSError:
        pass
