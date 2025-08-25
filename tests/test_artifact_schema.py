#!/usr/bin/env python3
"""
Artifact schema validation tests (Windows-first).

Uses the built-in sample artifact creator and validator to avoid external deps.
"""
from __future__ import annotations

from pathlib import Path

import sys

# Ensure src import path
sys.path.insert(0, str(Path(__file__).parent.parent))

from main import _create_sample_artifact, _validate_artifacts  # type: ignore
from config import config


def test_sample_artifact_conforms_to_schema():
    # Arrange: create a fresh sample artifact
    _create_sample_artifact()

    # Act + Assert: run validator; it raises SystemExit(0) on success
    try:
        _validate_artifacts(["--ignore-legacy"])  # legacy skip to be lenient on old files
    except SystemExit as e:  # pytest-friendly
        assert e.code == 0

    # Sanity check: at least one json artifact exists
    results_dir = Path(config.system.results_dir)
    artifacts = list(results_dir.glob("*.json"))
    assert artifacts, "Expected at least one artifact in results/"


