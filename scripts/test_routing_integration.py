"""Run offline integration coverage for canonical carrier routing.

Session routing now uses durable admission/activation/claim, never a gateway
session execution loop. Keep this diagnostic entry point tied to those actual
production seams.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root: Path = Path(__file__).resolve().parents[1]
    targets: list[str] = [
        "tests/test_affinity_fallback.py",
        "tests/test_turn_queue_producer1.py",
        "tests/test_a102_carrier_contract.py",
    ]
    return subprocess.run(
        [sys.executable, "-m", "pytest", *targets, "--tb=short"], cwd=root, check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
