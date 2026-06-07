#!/usr/bin/env python3
"""
PM2 entry point for the mesh worker daemon.

PM2 treats `script` as a real file path, so it cannot run `python -m
src.worker.agent` directly (it would look for a file literally named "-m").
This thin launcher mirrors main.py: it puts ./src on sys.path and hands off to
src.worker.agent.main(), which loads .env, inits the shared observability spine,
and runs the WorkerAgent.

Run directly (no PM2 required):
    python worker_main.py
"""
import sys
from pathlib import Path

# Match main.py: append (not prepend) src so we don't shadow third-party
# packages (e.g. telegram) that share a top-level name with our modules.
src_path = Path(__file__).parent / "src"
if str(src_path) not in sys.path:
    sys.path.append(str(src_path))

from src.worker.agent import main

if __name__ == "__main__":
    main()
