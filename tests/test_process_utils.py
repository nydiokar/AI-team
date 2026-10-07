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


def test_resolve_codex_prefers_path(monkeypatch):
    monkeypatch.setattr(process_utils.shutil, "which", lambda name, path=None: r"C:\npm\codex.cmd")
    assert process_utils.resolve_codex_executable({"PATH": r"C:\npm"}) == r"C:\npm\codex.cmd"


def test_resolve_codex_falls_back_to_standalone_install(monkeypatch, tmp_path):
    monkeypatch.setattr(process_utils.sys, "platform", "win32")
    monkeypatch.setattr(process_utils.shutil, "which", lambda name, path=None: None)
    exe = tmp_path / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(process_utils.ntpath, "join", os.path.join)

    assert process_utils.resolve_codex_executable({"PATH": "", "LOCALAPPDATA": str(tmp_path)}) == str(exe)


def test_resolve_codex_missing_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(process_utils.sys, "platform", "win32")
    monkeypatch.setattr(process_utils.shutil, "which", lambda name, path=None: None)

    assert process_utils.resolve_codex_executable({"PATH": "", "LOCALAPPDATA": str(tmp_path)}) is None
