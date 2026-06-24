"""Unit tests for the UI-4 artifact reader (src/control/artifacts.py).

Pure-function tests over a temp ``results/`` dir — no FastAPI, no network, no
paid backend.
"""
import json
import time
from pathlib import Path

from src.control import artifacts


def _write(results: Path, task_id: str, **fields) -> Path:
    p = results / f"{task_id}.json"
    body = {"task_id": task_id, "success": True, "timestamp": "2026-06-24T00:00:00"}
    body.update(fields)
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


def test_list_newest_first_and_limit(tmp_path):
    _write(tmp_path, "task_old", files_modified=["a.py"])
    time.sleep(0.01)
    _write(tmp_path, "task_new", files_modified=["b.py", "c.py"])
    # index.json is the orchestrator map, not an artifact — must be skipped.
    (tmp_path / "index.json").write_text("{}", encoding="utf-8")

    out = artifacts.list_artifacts(tmp_path, limit=50)
    ids = [a["task_id"] for a in out]
    assert ids == ["task_new", "task_old"]  # newest-first by mtime
    assert "index" not in " ".join(ids)
    assert out[0]["file_count"] == 2 and out[0]["has_changes"] is True

    assert len(artifacts.list_artifacts(tmp_path, limit=1)) == 1


def test_list_missing_dir_is_empty(tmp_path):
    assert artifacts.list_artifacts(tmp_path / "nope", limit=10) == []


def test_get_known_artifact(tmp_path):
    _write(tmp_path, "task_x", files_modified=["x.py"], execution_time=1.5,
           errors=[], parent_task_id=None)
    got = artifacts.get_artifact(tmp_path, "task_x")
    assert got is not None
    assert got["task_id"] == "task_x"
    assert got["files_modified"] == ["x.py"]
    assert got["execution_time"] == 1.5


def test_get_missing_is_none(tmp_path):
    assert artifacts.get_artifact(tmp_path, "task_nope") is None


def test_get_rejects_traversal(tmp_path):
    # A secret outside results/ must NOT be reachable via a ../ task_id.
    secret = tmp_path.parent / "secret.json"
    secret.write_text(json.dumps({"task_id": "secret"}), encoding="utf-8")
    results = tmp_path / "results"
    results.mkdir()
    assert artifacts.get_artifact(results, "../secret") is None
    assert artifacts.get_artifact(results, "..\\secret") is None
    # Drive-absolute / anchored task_id: Path('results') / 'C:/x' DISCARDS the left
    # operand and resolves to C:\x — the confinement check is what stops it, so this
    # is the load-bearing case. (Verified: without the check it would escape.)
    assert artifacts.get_artifact(results, "C:/Windows/System32/drivers/etc/hosts") is None
    assert artifacts.get_artifact(results, "/etc/passwd") is None
    # Degenerate ids that resolve to the dir itself, not a file inside it.
    assert artifacts.get_artifact(results, "") is None
    assert artifacts.get_artifact(results, ".") is None


def test_to_remote_files_prefers_file_changes(tmp_path):
    artifact = {
        "files_modified": ["flat.py"],
        "file_changes": [
            {"path": "a.py", "change_type": "untracked", "git_status": "??",
             "added_lines": 10, "deleted_lines": 0},
            {"path": "b.py", "change_type": "modified", "added_lines": 3,
             "deleted_lines": 1},
            {"path": "c.py", "change_type": "deleted", "added_lines": 0,
             "deleted_lines": 5},
        ],
    }
    files = artifacts.to_remote_files(artifact)
    assert [f["path"] for f in files] == ["a.py", "b.py", "c.py"]
    assert [f["change"] for f in files] == ["added", "modified", "deleted"]
    assert files[0]["added"] == 10


def test_to_remote_files_flat_fallback(tmp_path):
    artifact = {"files_modified": ["x.py", "y.py"]}
    files = artifacts.to_remote_files(artifact)
    assert [f["path"] for f in files] == ["x.py", "y.py"]
    assert all(f["change"] == "modified" for f in files)
    assert all(f["added"] is None and f["deleted"] is None for f in files)
