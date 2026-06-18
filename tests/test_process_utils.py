import os

from src.core import process_utils


def test_ensure_node_on_path_updates_both_windows_path_keys(monkeypatch):
    monkeypatch.setattr(process_utils.sys, "platform", "win32")

    env = {
        "Path": r"C:\Windows\System32",
        "PATH": r"C:\stale",
        "APPDATA": r"C:\Users\me\AppData\Roaming",
        "CODEX_NODE_PATH": r"D:\Tools\nodejs\node.exe",
    }

    updated = process_utils.ensure_node_on_path(env)

    expected_prefix = os.pathsep.join(
        [
            r"D:\Tools\nodejs",
            r"C:\Program Files\nodejs",
            r"C:\Users\me\AppData\Roaming\npm",
        ]
    )
    assert updated["Path"].startswith(expected_prefix)
    assert updated["PATH"] == updated["Path"]
    assert updated["Path"].endswith(r"C:\Windows\System32")


def test_ensure_node_on_path_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(process_utils.sys, "platform", "linux")
    env = {"PATH": "/usr/bin"}

    assert process_utils.ensure_node_on_path(env) == env
