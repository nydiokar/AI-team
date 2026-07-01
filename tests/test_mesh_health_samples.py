import json
from datetime import datetime, timedelta
from pathlib import Path

from src.control.db import MeshDB


def test_record_mesh_health_sample_captures_current_mesh_load(tmp_path: Path) -> None:
    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_node("node-a", "127.0.0.1", 9001, ["claude"], 4, status="online")
    db.heartbeat_node(
        "node-a",
        json.dumps(
            {
                "slots_used": 2,
                "slots_total": 4,
                "active_tasks": ["task-a", "task-b"],
            }
        ),
    )
    db.enqueue_task(
        task_id="task-pending",
        session_id=None,
        machine_id=None,
        backend="claude",
        action="run_oneoff",
        payload={"prompt": "pending"},
    )

    sample = db.record_mesh_health_sample(source="test")

    assert sample["source"] == "test"
    assert sample["nodes_online"] == 1
    assert sample["nodes_total"] == 1
    assert sample["slots_used"] == 2
    assert sample["slots_total"] == 4
    assert sample["slots_available"] == 2
    assert sample["active_tasks"] == 2
    assert sample["tasks_pending"] == 1
    assert sample["stale_live_state_nodes"] == []

    recent = db.list_mesh_health_samples(limit=10)
    assert len(recent) == 1
    assert recent[0]["id"] == sample["id"]
    assert recent[0]["source"] == "test"


def test_mesh_health_sample_retention_prunes_old_rows(tmp_path: Path) -> None:
    db = MeshDB(str(tmp_path / "mesh.db"))
    old_time = (datetime.utcnow() - timedelta(hours=3)).isoformat()
    db._conn().execute(
        """
        INSERT INTO mesh_health_samples (
            sampled_at, source, stale_live_state_nodes_json
        ) VALUES (?, ?, ?)
        """,
        (old_time, "old", "[]"),
    )
    db.record_mesh_health_sample(source="new")

    db.prune_mesh_health_samples(retention_hours=1, max_rows=100)

    samples = db.list_mesh_health_samples(limit=10)
    assert [sample["source"] for sample in samples] == ["new"]
