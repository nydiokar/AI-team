"""
Unit/integration tests for scripts/repo_index/symbol_lookup.py.

These exercise the real `universal-ctags` binary against throwaway repos in tmp_path —
they do NOT touch the Claude CLI and cost nothing. Skipped entirely if ctags/readtags
are not installed.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT: Path = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "repo_index" / "symbol_lookup.py"
)

_HAVE_CTAGS: bool = bool(shutil.which("ctags")) and bool(shutil.which("readtags"))
pytestmark = pytest.mark.skipif(
    not _HAVE_CTAGS, reason="universal-ctags not installed"
)


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=root, capture_output=True, text=True,
    )


def test_exact_lookup_resolves_file_and_line(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "mod.py").write_text("class WidgetFactory:\n    def build_widget(self):\n        return 1\n")
    out = _run(tmp_path, "--defs-only", "WidgetFactory").stdout
    assert "src/mod.py:1" in out
    assert "(c)" in out


def test_miss_returns_fuzzy_suggestion(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "mod.py").write_text("def _dispatch_worker():\n    pass\n")
    out = _run(tmp_path, "dispatch_worker").stdout
    assert "did you mean" in out
    assert "_dispatch_worker" in out


def test_stale_detects_content_edit(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    f = src / "mod.py"
    f.write_text("def alpha():\n    pass\n")
    _run(tmp_path, "--build")
    assert _run(tmp_path, "--stale").stdout.strip() == "FRESH"
    # Edit content -> file mtime advances -> STALE.
    import os
    future = f.stat().st_mtime + 10
    f.write_text("def alpha():\n    return 2\n")
    os.utime(f, (future, future))
    assert _run(tmp_path, "--stale").stdout.strip() == "STALE"


def test_stale_detects_file_deletion(tmp_path: Path) -> None:
    """The classic gap: deleting a file leaves the index pointing at a ghost symbol."""
    _init_repo(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("def alpha():\n    pass\n")
    (src / "b.py").write_text("def beta():\n    pass\n")
    _run(tmp_path, "--build")
    assert _run(tmp_path, "--stale").stdout.strip() == "FRESH"
    import os
    future = tmp_path.stat().st_mtime + 10
    (src / "b.py").unlink()          # deletion bumps the directory mtime
    os.utime(src, (future, future))
    assert _run(tmp_path, "--stale").stdout.strip() == "STALE"


def test_auto_detects_new_source_dir(tmp_path: Path) -> None:
    """A non-standard top-level source dir must be indexed without a .ctags_dirs pin."""
    _init_repo(tmp_path)
    services = tmp_path / "services"      # not in the hardcoded candidate list
    services.mkdir()
    (services / "svc.py").write_text("def reconcile_ledger():\n    pass\n")
    dirs = _run(tmp_path, "--dirs").stdout
    assert "services" in dirs
    out = _run(tmp_path, "--defs-only", "reconcile_ledger").stdout
    assert "services/svc.py:1" in out


def test_nested_source_dir_collapsed(tmp_path: Path) -> None:
    """'web/src' must collapse into 'web' so files are not indexed twice."""
    _init_repo(tmp_path)
    web_src = tmp_path / "web" / "src"
    web_src.mkdir(parents=True)
    (web_src / "app.ts").write_text("export function boot() { return 1 }\n")
    dirs = _run(tmp_path, "--dirs").stdout.split()
    assert "web" in dirs
    assert "web/src" not in dirs


def test_vendored_dirs_excluded_from_index(tmp_path: Path) -> None:
    """ctags -R must NOT descend into node_modules/dist — else the index explodes."""
    _init_repo(tmp_path)
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    (web / "src" / "app.ts").write_text("export function realBoot() { return 1 }\n")
    vendor = web / "node_modules" / "junk"
    vendor.mkdir(parents=True)
    (vendor / "junk.ts").write_text("export function vendoredGhost() { return 9 }\n")
    _run(tmp_path, "--build")
    assert "realBoot" in _run(tmp_path, "--no-auto", "realBoot").stdout
    ghost = _run(tmp_path, "--no-auto", "vendoredGhost").stdout
    assert "web/node_modules" not in ghost   # not indexed at all


def test_non_source_dir_not_indexed(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("x = 1\n")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "readme.txt").write_text("no code here\n")
    dirs = _run(tmp_path, "--dirs").stdout.split()
    assert "docs" not in dirs
