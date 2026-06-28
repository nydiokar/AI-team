from src.control.db import MeshDB


def test_pending_tasks_can_exclude_unpinned_work(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    db.enqueue_task(
        task_id="task_unpinned",
        session_id=None,
        machine_id=None,
        backend="codex",
        action="run_oneoff",
        payload={"prompt": "do not run"},
    )
    db.enqueue_task(
        task_id="task_pinned",
        session_id=None,
        machine_id="smoke-node",
        backend="codex",
        action="run_oneoff",
        payload={"prompt": "smoke"},
    )

    default_rows = db.get_pending_tasks(node_id="smoke-node", backends=["codex"])
    pinned_only_rows = db.get_pending_tasks(
        node_id="smoke-node",
        backends=["codex"],
        accept_unpinned=False,
    )

    assert [row["id"] for row in default_rows] == ["task_unpinned", "task_pinned"]
    assert [row["id"] for row in pinned_only_rows] == ["task_pinned"]
