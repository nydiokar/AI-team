"""Task-server `/files` staging safety — regression for security review P0-1.

The upload endpoint must never write outside its staging root: the client-supplied
filename is a path segment and a `../../` name previously escaped `_STAGING_ROOT`
(arbitrary file write on the gateway host). No network, no paid backend.
"""
import pytest
from fastapi.testclient import TestClient

import src.control.task_server as ts


TOKEN = "test-stage-token"


@pytest.fixture
def staging(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "_STAGING_ROOT", tmp_path)
    monkeypatch.setattr(ts, "_worker_token", lambda: TOKEN)
    return tmp_path


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def test_files_requires_auth(staging):
    client = TestClient(ts.app)
    r = client.post("/files", files={"file": ("safe.txt", b"hello")})
    assert r.status_code in (401, 403)


def test_stage_file_writes_inside_staging_root(staging):
    client = TestClient(ts.app)
    r = client.post("/files", headers=_auth(), files={"file": ("safe.txt", b"hello")})
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "safe.txt"
    staged = list((staging / body["file_id"]).glob("*"))
    assert len(staged) == 1
    assert staged[0].read_bytes() == b"hello"


def test_stage_file_rejects_path_traversal(staging):
    """P0-1 regression: `../../` in the filename must not escape the staging root.

    Sanitization neutralises the traversal (the stored name is safe), so the
    endpoint returns 200 — what matters is that nothing lands outside the root.
    """
    client = TestClient(ts.app)
    r = client.post("/files", headers=_auth(),
                    files={"file": ("../../pwned.txt", b"PWNED")})
    assert r.status_code == 200
    body = r.json()
    assert "/" not in body["filename"] and "\\" not in body["filename"]
    assert not (staging.parent / "pwned.txt").exists()
    assert not (staging / "pwned.txt").exists()
    staged = list((staging / body["file_id"]).glob("*"))
    assert len(staged) == 1
    assert staged[0].parent == staging / body["file_id"]
    assert staged[0].read_bytes() == b"PWNED"


def test_stage_file_sanitizes_hostile_chars(staging):
    client = TestClient(ts.app)
    r = client.post("/files", headers=_auth(),
                    files={"file": ("a/b\\c:d e?.txt", b"x")})
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "a_b_c_d_e_.txt"
    staged = list((staging / body["file_id"]).glob("*"))
    assert len(staged) == 1
    assert staged[0].name == "a_b_c_d_e_.txt"


def test_stage_file_containment_rejects_dotdot_name(staging):
    """A pure-dot name (e.g. `..`) survives the charset; the fallback rule maps it
    to a safe literal instead of writing to a parent directory."""
    client = TestClient(ts.app)
    r = client.post("/files", headers=_auth(), files={"file": ("..", b"x")})
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "upload"
    staged = list((staging / body["file_id"]).glob("*"))
    assert len(staged) == 1
    assert staged[0].parent == staging / body["file_id"]


def test_stage_file_then_fetch(staging):
    client = TestClient(ts.app)
    r = client.post("/files", headers=_auth(), files={"file": ("data.bin", b"\x00\x01\x02")})
    file_id = r.json()["file_id"]
    got = client.get(f"/files/{file_id}", headers=_auth())
    assert got.status_code == 200
    assert got.content == b"\x00\x01\x02"
