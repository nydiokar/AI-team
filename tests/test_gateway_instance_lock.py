"""Gateway single-instance lock: a stale lock naming our own pid must not self-kill.

In a container restart the pid namespace is recycled, so the stale lock left by
the previous (crashed) start can name the new process's own pid with a
create_time inside the 2 s match tolerance. Treating that as a rival made the
gateway terminate itself on every start (restart loop, exit 143).
"""
import json
import os
from pathlib import Path

import main  # type: ignore
from src.core.process_utils import current_process_create_time


def _write_lock(path: Path, pid: int) -> None:
    path.write_text(json.dumps({
        "pid": pid,
        "create_time": current_process_create_time(),
        "root": str(Path(main.__file__).resolve().parent),
        "entrypoint": str(Path(main.__file__).resolve()),
    }))


def test_stale_lock_with_own_pid_is_replaced_not_terminated(tmp_path, monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(main, "terminate_process_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(main, "process_matches_entrypoint", lambda *a, **k: True)
    lock_path = tmp_path / "gateway.lock"
    _write_lock(lock_path, os.getpid())

    lock = main._GatewayInstanceLock(lock_path, Path(main.__file__).resolve().parent)
    lock.acquire()

    assert killed == []
    assert json.loads(lock_path.read_text())["pid"] == os.getpid()


def test_stale_lock_with_parent_pid_is_replaced_not_terminated(tmp_path, monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(main, "terminate_process_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(main, "process_matches_entrypoint", lambda *a, **k: True)
    lock_path = tmp_path / "gateway.lock"
    _write_lock(lock_path, os.getppid())

    main._GatewayInstanceLock(lock_path, Path(main.__file__).resolve().parent).acquire()

    assert killed == []


def test_lock_naming_another_live_gateway_still_terminates_it(tmp_path, monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(main, "terminate_process_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(main, "process_matches_entrypoint", lambda *a, **k: True)
    lock_path = tmp_path / "gateway.lock"
    rival = 999_999
    _write_lock(lock_path, rival)

    main._GatewayInstanceLock(lock_path, Path(main.__file__).resolve().parent).acquire()

    assert killed == [rival]
