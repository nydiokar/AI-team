"""Reject unsupported service interpreters before importing runtime modules."""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("version", ["(3, 10)", "(3, 11)", "(3, 12)", "(3, 13)"])
def test_runtime_python_minimum(version: str) -> None:
    package: Path = Path(__file__).resolve().parents[1] / "src" / "__init__.py"
    result = subprocess.run(
        [sys.executable, "-c",
         f"import runpy, sys; sys.version_info = {version}; runpy.run_path(sys.argv[1])",
         str(package)],
        capture_output=True, text=True, timeout=5,
    )
    if version in ("(3, 10)", "(3, 11)"):
        assert result.returncode != 0
        assert "requires Python 3.12 or newer" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
