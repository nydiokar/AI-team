"""Cross-platform process utilities for gateway/runtime lifecycle."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


def ensure_node_on_path(env: Optional[dict] = None) -> dict:
    """Return an env dict with Node.js directories prepended to PATH on Windows.

    Node-based CLIs (codex, opencode) installed via npm use ``.cmd`` shims
    that invoke ``node``. If the parent process's PATH lacks the Node.js
    install directory, Windows ``cmd.exe`` fails with::

        '"node"' is not recognized as an internal or external command

    This is a well-known Windows PATH-inheritance problem: PM2 worker
    processes started via ``pm2 resurrect`` inherit a stale PATH that
    lacks the Node.js bin directories.  ``pm2-ressurect.bat`` works around
    it at the PM2 level, but children spawned by the worker still inherit
    the worker's PATH, so every backend that launches a Node-based CLI
    must call this helper to build the child's environment.

    The function is a no-op on non-Windows platforms.
    """
    if env is None:
        env = os.environ.copy()
    else:
        env = dict(env)

    if sys.platform != "win32":
        return env

    path = env.get("PATH", "")
    appdata = env.get("APPDATA", "")
    node_dirs = [r"C:\Program Files\nodejs"]
    if appdata:
        node_dirs.append(os.path.join(appdata, "npm"))

    path_parts = path.split(os.pathsep)
    additions = [d for d in node_dirs if d not in path_parts]
    if additions:
        env["PATH"] = os.pathsep.join(additions) + os.pathsep + path

    return env


def pid_exists(pid: int) -> bool:
    """Return True when a process id is currently alive."""
    if pid <= 0:
        return False
    if psutil:
        try:
            return psutil.pid_exists(pid)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return True
    return True


def current_process_create_time() -> float:
    """Best-effort create time for the current process."""
    if psutil:
        try:
            return psutil.Process(os.getpid()).create_time()
        except Exception:
            return 0.0
    return 0.0


def process_matches_entrypoint(
    pid: int,
    *,
    started: float = 0.0,
    app_root: Path,
    entrypoint: Optional[Path] = None,
) -> bool:
    """Check whether a live process looks like the same gateway app."""
    if not pid_exists(pid):
        return False
    if not psutil:
        return True

    try:
        proc = psutil.Process(pid)
        if started and abs(proc.create_time() - started) > 2:
            return False

        root_path = str(app_root.resolve()).lower()
        main_path = str((entrypoint or (app_root / "main.py")).resolve()).lower()
        cmdline = [str(part).lower() for part in proc.cmdline()]
        try:
            proc_cwd = str(Path(proc.cwd()).resolve()).lower()
        except Exception:
            proc_cwd = ""

        return (
            any("main.py" in part or main_path in part for part in cmdline)
            and (proc_cwd == root_path or any(root_path in part for part in cmdline))
        )
    except Exception:
        return False


def terminate_process_tree(pid: int, timeout: float = 8.0) -> None:
    """Best-effort recursive process termination for Windows and Linux."""
    if pid <= 0:
        return

    if psutil:
        try:
            proc = psutil.Process(pid)
            children = proc.children(recursive=True)
            for child in children:
                with contextlib.suppress(Exception):
                    child.terminate()
            proc.terminate()
            _, alive = psutil.wait_procs([proc, *children], timeout=timeout)
            for item in alive:
                with contextlib.suppress(Exception):
                    item.kill()
            return
        except Exception:
            pass

    sig = signal.SIGTERM if os.name != "nt" else signal.SIGTERM
    with contextlib.suppress(Exception):
        os.kill(pid, sig)


def terminate_subprocess_tree(proc: subprocess.Popen, timeout: float = 8.0) -> None:
    """Terminate a running subprocess and any descendants."""
    if proc is None:
        return
    with contextlib.suppress(Exception):
        if proc.poll() is not None:
            return
    terminate_process_tree(proc.pid, timeout=timeout)


def terminate_many_popen(procs: Iterable[subprocess.Popen], timeout: float = 8.0) -> None:
    """Terminate a collection of subprocess handles."""
    for proc in list(procs):
        terminate_subprocess_tree(proc, timeout=timeout)
