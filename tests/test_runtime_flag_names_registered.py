"""Static guard: every flag name read through the registry seam is registered.

``runtime_flag_enabled()`` / ``runtime_flag_value()`` raise ``ValueError`` on an
unknown name (fail-fast on typos). That is only safe if no caller can reach a name
that was removed from ``RUNTIME_FLAG_DEFINITIONS`` — which is exactly what a
rollout-flag retirement risks (delete the definition, miss one caller → runtime
crash on a rarely-hit path). This test turns that into a test-time failure.
"""
from __future__ import annotations

import re
from pathlib import Path

from src.control.db import RUNTIME_FLAG_DEFINITIONS

_REPO: Path = Path(__file__).resolve().parents[1]
_SCAN_DIRS: tuple[str, ...] = ("src", "scripts", "config")
_READ_CALL: re.Pattern[str] = re.compile(
    r"runtime_flag_(?:enabled|value)\(\s*[\"']([A-Z][A-Z0-9_]*)[\"']"
)


def _flag_reads() -> dict[str, list[str]]:
    reads: dict[str, list[str]] = {}
    for top in _SCAN_DIRS:
        for path in (_REPO / top).rglob("*.py"):
            text: str = path.read_text(encoding="utf-8", errors="replace")
            for match in _READ_CALL.finditer(text):
                reads.setdefault(match.group(1), []).append(str(path.relative_to(_REPO)))
    return reads


def test_scan_finds_registry_reads() -> None:
    # Sanity: the regex must actually see the seam, or the guard below is vacuous.
    # Pinned to long-lived operator knobs, not a count (rollout flags keep retiring).
    reads = _flag_reads()
    assert {"CACHE_HEARTBEAT_ACTIVE", "QUOTA_PREWARM_ENABLED"} <= set(reads)


def test_every_flag_read_is_registered() -> None:
    unknown: dict[str, list[str]] = {
        name: files for name, files in _flag_reads().items()
        if name not in RUNTIME_FLAG_DEFINITIONS
    }
    assert not unknown, f"flag reads with no RUNTIME_FLAG_DEFINITIONS entry: {unknown}"
