"""Cross-platform process utilities for gateway/runtime lifecycle."""

from __future__ import annotations

import contextlib
import ntpath
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

    path_key = "Path" if "Path" in env else "PATH"
    path = env.get(path_key) or env.get("PATH") or env.get("Path") or ""
    appdata = env.get("APPDATA", "")
    node_dirs = []
    node_hint = env.get("CODEX_NODE_PATH") or env.get("NODE_EXE")
    if node_hint:
        # Use Windows path semantics even when this branch is unit-tested on a
        # non-Windows host. pathlib.Path would interpret backslashes as plain
        # characters and incorrectly return "." as the parent.
        node_dirs.append(ntpath.dirname(node_hint))
    node_dirs.append(r"C:\Program Files\nodejs")
    if appdata:
        node_dirs.append(ntpath.join(appdata, "npm"))

    path_parts = path.split(os.pathsep)
    additions = [d for d in node_dirs if d not in path_parts]
    if additions:
        path = os.pathsep.join(additions) + os.pathsep + path

    env["PATH"] = path
    env["Path"] = path

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


# Env var stamped into every worker-spawned backend child (inherited from the
# worker's os.environ). A boot reaper uses it to distinguish THIS worker
# incarnation's live children from prior-incarnation orphans.
WORKER_INCARNATION_ENV = "WORKER_INCARNATION_ID"


def reap_stale_worker_children(
    current_incarnation: str,
    *,
    names: Iterable[str] = ("claude", "claude.exe"),
    env_key: str = WORKER_INCARNATION_ENV,
) -> list[int]:
    """Kill backend child processes left behind by a PRIOR worker incarnation.

    A worker-spawned backend (e.g. the Claude SDK ``claude`` process) communicates
    over stdin/stdout pipes owned by the worker process; when that worker dies the
    pipes die with it, so the surviving child is permanently unreachable — it can
    never receive another turn. Keeping it alive preserves nothing and leaks RAM +
    an auth/token slot. On a fresh worker boot we therefore reap any such child
    whose stamped incarnation differs from ours.

    Selection is deliberately narrow: a process is reaped ONLY if it carries the
    ``env_key`` env var AND its value is non-empty AND != ``current_incarnation``.
    Interactive ``claude`` sessions a human launched never carry the stamp, so they
    are never touched; nor are this incarnation's own children.

    Returns the list of reaped pids. No-op (returns ``[]``) when psutil is absent
    or ``current_incarnation`` is empty.
    """
    if not current_incarnation or psutil is None:
        return []
    wanted = {n.lower() for n in names}
    reaped: list[int] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name not in wanted:
                continue
            stamp = proc.environ().get(env_key, "")
            if not stamp or stamp == current_incarnation:
                continue
        except Exception:
            # environ() can raise (permission, process gone) — skip, never guess.
            continue
        try:
            terminate_process_tree(proc.info["pid"])
            reaped.append(int(proc.info["pid"]))
        except Exception:
            continue
    return reaped
