from pathlib import Path
import shutil
import uuid

from src.core.path_resolver import PathResolver


def _make_workspace() -> Path:
    root = Path.cwd() / ".test_path_resolver" / uuid.uuid4().hex[:8]
    (root / "repo-a" / "src").mkdir(parents=True, exist_ok=True)
    (root / "repo-b").mkdir(parents=True, exist_ok=True)
    return root


def test_resolve_relative_path_within_allowed_root():
    root = _make_workspace()
    try:
        resolver = PathResolver(base_cwd=str(root), allowed_root=str(root))
        result = resolver.resolve_session_path("repo-a")
        assert result.ok is True
        assert result.resolved_path == str((root / "repo-a").resolve())
        assert "src" in result.available_dirs
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


def test_missing_path_returns_suggestions():
    root = _make_workspace()
    try:
        resolver = PathResolver(base_cwd=str(root), allowed_root=str(root))
        result = resolver.resolve_session_path("repo-aa")
        assert result.ok is False
        assert result.error == "Path does not exist."
        assert str((root / "repo-a").resolve()) in result.suggestions
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


def test_execution_path_falls_back_to_base_for_invalid_input():
    root = _make_workspace()
    try:
        resolver = PathResolver(base_cwd=str(root), allowed_root=str(root))
        resolved = resolver.resolve_execution_path("does-not-exist")
        assert resolved == str(root.resolve())
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)
