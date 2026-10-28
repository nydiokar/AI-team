"""Bounded Home read models over canonical sessions and sanitized telemetry.

No provider calls or writes. Session counts include the full open population;
only previews are capped. Managed activity follows the turn ledger, not stale BUSY.
"""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from src.control.db import MeshDB

ActivityState = Literal['running', 'queued', 'waiting', 'failed', 'idle']


class ActivitySession(BaseModel):
    session_id: str
    backend: str
    state: ActivityState
    summary: str
    repo_path: str


class NodeActivity(BaseModel):
    counts: dict[ActivityState, int] = Field(default_factory=lambda: {
        'running': 0, 'queued': 0, 'waiting': 0, 'failed': 0, 'idle': 0,
    })
    sessions: list[ActivitySession] = Field(default_factory=list)


def node_activity(db: MeshDB, node_ids: list[str]) -> dict[str, NodeActivity]:
    if not node_ids:
        return {}
    # One SQL read for all nodes; full counts + three previews per state. The
    # open-turn partial index keeps historical mesh_tasks out of the join.
    marks: str = ','.join('?' for _ in node_ids)
    rows = db._conn().execute(f"""
        WITH queue AS (
            SELECT session_id,
                   MAX(status IN ('pending','claimed','running')) AS active,
                   MAX(status = 'recovery_required') AS recovery,
                   MAX(status = 'queued') AS queued
            FROM mesh_tasks
            WHERE queue_protocol = 1 AND status IN
                ('queued','pending','claimed','running','recovery_required')
            GROUP BY session_id
        ), activity AS (
            SELECT s.session_id, s.machine_id, s.backend, substr(s.repo_path,1,512) AS repo_path,
                   substr(COALESCE(NULLIF(s.last_summary,''), s.last_user_message),1,160) AS summary,
                   s.updated_at,
                   CASE
                     WHEN s.turn_queue_enrolled = 1 AND q.recovery = 1 THEN 'failed'
                     WHEN s.turn_queue_enrolled = 1 AND q.active = 1 THEN 'running'
                     WHEN s.status IN ('error','pinned_node_offline') THEN 'failed'
                     WHEN s.turn_queue_enrolled = 1 AND q.queued = 1 THEN 'queued'
                     WHEN s.turn_queue_enrolled = 0 AND s.status IN
                          ('busy','paused_pinned_node_offline') THEN 'running'
                     WHEN s.status = 'awaiting_input' THEN 'waiting'
                     ELSE 'idle'
                   END AS state
            FROM sessions s LEFT JOIN queue q ON q.session_id = s.session_id
            WHERE s.status != 'closed' AND s.machine_id IN ({marks})
        ), ranked AS (
            SELECT *, COUNT(*) OVER (PARTITION BY machine_id,state) AS count,
                   ROW_NUMBER() OVER (PARTITION BY machine_id,state
                                      ORDER BY updated_at DESC,session_id) AS rank
            FROM activity
        ) SELECT * FROM ranked WHERE rank <= 3
    """, node_ids).fetchall()
    result: dict[str, NodeActivity] = {node_id: NodeActivity() for node_id in node_ids}
    for row in rows:
        session: ActivitySession = ActivitySession.model_validate(dict(row))
        activity: NodeActivity = result[row['machine_id']]
        activity.counts[session.state] = row['count']
        activity.sessions.append(session)
    return result


class ObservedUsage(BaseModel):
    provider: Literal['codex'] = 'codex'
    node_id: str
    observed_at: str
    window_duration_seconds: int
    used_percent: float
    reset_at: str
    plan_type: str | None = None


def codex_usage(db: MeshDB, *, now: datetime | None = None) -> list[ObservedUsage]:
    """Latest per-node/window native limits, valid for 15 min and until reset.

    Node identity is NOT an account identity. Consumers may take the highest
    reading across nodes, but must never add readings or infer account counts.
    Uses the event-name/time index with a 2000-row/16 KiB-per-row read bound.
    """
    now = now or datetime.now(UTC)
    since: str = (now - timedelta(minutes=15)).isoformat().replace('+00:00', 'Z')
    rows = db._conn().execute("""
        SELECT node_id,event_time,substr(attributes,1,16384) AS attributes
        FROM llm_events WHERE event_name='model.session_usage' AND event_time >= ?
          AND backend='codex' ORDER BY event_time DESC,source_sequence DESC LIMIT 2000
    """, (since,)).fetchall()
    result: list[ObservedUsage] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        try:
            attrs = json.loads(row['attributes'])
            observed: datetime = datetime.fromisoformat(row['event_time'])
            if observed.tzinfo is None or observed < now - timedelta(minutes=15) or observed > now + timedelta(seconds=60):
                continue
        except (ValueError, TypeError):
            continue
        if not isinstance(attrs, dict):
            continue
        for window in ('primary', 'secondary'):
            key: tuple[str, str] = (row['node_id'], window)
            prefix: str = f'rate_limit_{window}_'
            if key in seen or prefix + 'used_percent' not in attrs:
                continue
            seen.add(key)
            percent: object = attrs.get(prefix + 'used_percent')
            minutes: object = attrs.get(prefix + 'window_minutes')
            reset: object = attrs.get(prefix + 'resets_at')
            if (type(percent) not in (int, float) or not math.isfinite(percent)
                    or not 0 <= percent <= 100 or type(minutes) is not int
                    or minutes not in (300, 10080) or type(reset) not in (int, float)):
                continue
            try:
                reset_at: datetime = datetime.fromtimestamp(reset, UTC)
            except (ValueError, OverflowError, OSError):
                continue
            if reset_at <= now:
                continue
            plan: object = attrs.get('rate_limit_plan_type')
            result.append(ObservedUsage(node_id=row['node_id'], observed_at=row['event_time'],
                window_duration_seconds=minutes * 60, used_percent=percent,
                reset_at=reset_at.isoformat(), plan_type=plan[:40] if isinstance(plan, str) else None))
    return result
