"""Home projections use ledger truth and bounded, fresh provider observations."""
from datetime import datetime, timedelta, timezone

from src.control.db import MeshDB
from src.control.home_status import node_activity, codex_usage
from src.control.telemetry_store import TelemetryStore
from src.core.telemetry import build_event


def test_node_counts_include_old_sessions_and_use_managed_ledger(tmp_path):
    db = MeshDB(str(tmp_path / 'mesh.db'))
    conn = db._conn()
    for i in range(230):
        conn.execute("INSERT INTO sessions(session_id,backend,repo_path,status,created_at,updated_at,machine_id,turn_queue_enrolled) VALUES(?,?,?,?,?,?,?,?)",
                     (f's{i}', 'codex', '/repo', 'busy', '2020', '2020', 'node', 1))
    conn.execute("UPDATE sessions SET status='error' WHERE session_id='s0'")
    conn.execute("UPDATE sessions SET status='closed' WHERE session_id='s1'")
    conn.execute("INSERT INTO mesh_tasks(id,session_id,backend,action,status,queue_protocol,created_at,updated_at,payload) VALUES('turn','s2','codex','resume_session','running',1,'2020','2020','{}')")
    conn.commit()
    activity = node_activity(db, ['node'])['node']
    assert activity.counts == {'running': 1, 'queued': 0, 'waiting': 0, 'failed': 1, 'idle': 227}
    assert len(activity.sessions) <= 15  # at most three previews per state
    assert any(s.session_id == 's2' and s.state == 'running' for s in activity.sessions)


def test_codex_usage_expires_and_does_not_turn_context_into_plan_usage(tmp_path):
    db = MeshDB(str(tmp_path / 'mesh.db'))
    now = datetime.now(timezone.utc)
    event = build_event('model.session_usage', turn_id='turn', session_id='s',
                        invocation_id='inv', node_id='node', backend='codex',
                        emitter_process_instance_id='test', source='backend', event_time=now,
                        attributes={'context_window_tokens': 100000,
                                    'rate_limit_primary_used_percent': 31,
                                    'rate_limit_primary_window_minutes': 300,
                                    'rate_limit_primary_resets_at': int((now + timedelta(hours=2)).timestamp())})
    TelemetryStore(db).insert_events([event], rebuild=False)
    usage = codex_usage(db, now=now)
    assert len(usage) == 1 and usage[0].used_percent == 31
    assert usage[0].window_duration_seconds == 18000
    assert codex_usage(db, now=now + timedelta(minutes=16)) == []


def test_codex_usage_chooses_latest_per_node_window_and_rejects_invalid(tmp_path):
    db = MeshDB(str(tmp_path / 'mesh.db'))
    now = datetime.now(timezone.utc)
    for i, pct in enumerate([90, 12, 101]):
        event = build_event('model.session_usage', turn_id='turn', node_id='node',
                            backend='codex', emitter_process_instance_id='test', source='backend',
                            source_sequence=i, event_time=now + timedelta(seconds=i),
                            attributes={'rate_limit_primary_used_percent': pct,
                                        'rate_limit_primary_window_minutes': 300,
                                        'rate_limit_primary_resets_at': int((now + timedelta(hours=1)).timestamp())})
        TelemetryStore(db).insert_events([event], rebuild=False)
    # Invalid latest evidence must not revive an earlier apparently valid reading.
    assert codex_usage(db, now=now + timedelta(seconds=3)) == []
