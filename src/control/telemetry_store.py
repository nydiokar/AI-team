"""Durable telemetry event storage and deterministic turn projections."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from src.control.db import MeshDB
from src.core.telemetry import TelemetryEvent
from src.core.telemetry_projection import project_turn


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class TelemetryStore:
    """Thin telemetry-specific repository over the existing MeshDB connection."""

    def __init__(self, db: MeshDB) -> None:
        self.db = db

    def insert_events(
        self, events: Iterable[TelemetryEvent | Dict[str, Any]], *, rebuild: bool = True
    ) -> Dict[str, Any]:
        validated = [
            event if isinstance(event, TelemetryEvent) else TelemetryEvent.model_validate(event)
            for event in events
        ]
        if not validated:
            return {"accepted": 0, "duplicates": 0, "rejected": 0, "turn_ids": []}

        accepted = 0
        duplicates = 0
        turn_ids = sorted({event.turn_id for event in validated})
        received_at = _now()
        with self.db._write() as conn:
            for event in validated:
                payload = event.model_dump(mode="json")
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO llm_events (
                        event_id, schema_version, event_name, event_time, observed_time,
                        node_id, emitter_process_instance_id, source, source_sequence,
                        clock_quality,
                        session_id, turn_id, invocation_id, model_request_id,
                        tool_call_id, subagent_id, backend, model, pid, attributes,
                        received_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["event_id"],
                        payload["schema_version"],
                        payload["event_name"],
                        payload["event_time"],
                        payload["observed_time"],
                        payload["node_id"],
                        payload["emitter_process_instance_id"],
                        payload["source"],
                        payload["source_sequence"],
                        payload["clock_quality"],
                        payload["session_id"],
                        payload["turn_id"],
                        payload["invocation_id"],
                        payload["model_request_id"],
                        payload["tool_call_id"],
                        payload["subagent_id"],
                        payload["backend"],
                        payload["model"],
                        payload["pid"],
                        _json(payload["attributes"]),
                        received_at,
                    ),
                )
                if cursor.rowcount:
                    accepted += 1
                else:
                    duplicates += 1

        if rebuild:
            for turn_id in turn_ids:
                self.rebuild_turn(turn_id)
        return {
            "accepted": accepted,
            "duplicates": duplicates,
            "rejected": 0,
            "turn_ids": turn_ids,
        }

    def list_events(
        self, turn_id: str, *, after: Optional[str] = None, limit: int = 1000
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 5000))
        if after:
            rows = self.db._conn().execute(
                """
                SELECT * FROM llm_events
                WHERE turn_id = ? AND event_time > ?
                ORDER BY event_time, source, COALESCE(source_sequence, 0), event_id
                LIMIT ?
                """,
                (turn_id, after, limit),
            ).fetchall()
        else:
            rows = self.db._conn().execute(
                """
                SELECT * FROM llm_events
                WHERE turn_id = ?
                ORDER BY event_time, source, COALESCE(source_sequence, 0), event_id
                LIMIT ?
                """,
                (turn_id, limit),
            ).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["attributes"] = json.loads(item.get("attributes") or "{}")
            except Exception:
                item["attributes"] = {}
            result.append(item)
        return result

    def rebuild_turn(self, turn_id: str) -> Optional[Dict[str, Any]]:
        events = self._all_events(turn_id)
        if not events:
            return None
        projection = project_turn(events)
        turn = projection["turn"]
        now = _now()

        with self.db._write() as conn:
            conn.execute(
                """
                INSERT INTO llm_turns (
                    turn_id, session_id, task_id, gateway_node_id, execution_node_id,
                    backend, backend_session_id_start, backend_session_id_end,
                    requested_model, observed_models, started_at, ended_at,
                    final_status, timeout_status, final_exit_code, final_invocation_id,
                    metrics_json, coverage_json, data_quality_json, projection_version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    task_id = excluded.task_id,
                    gateway_node_id = excluded.gateway_node_id,
                    execution_node_id = excluded.execution_node_id,
                    backend = excluded.backend,
                    backend_session_id_start = excluded.backend_session_id_start,
                    backend_session_id_end = excluded.backend_session_id_end,
                    requested_model = excluded.requested_model,
                    observed_models = excluded.observed_models,
                    started_at = excluded.started_at,
                    ended_at = excluded.ended_at,
                    final_status = excluded.final_status,
                    timeout_status = excluded.timeout_status,
                    final_exit_code = excluded.final_exit_code,
                    final_invocation_id = excluded.final_invocation_id,
                    metrics_json = excluded.metrics_json,
                    coverage_json = excluded.coverage_json,
                    data_quality_json = excluded.data_quality_json,
                    projection_version = excluded.projection_version,
                    updated_at = excluded.updated_at
                """,
                (
                    turn["turn_id"],
                    turn["session_id"],
                    turn["task_id"],
                    turn["gateway_node_id"],
                    turn["execution_node_id"],
                    turn["backend"],
                    turn.get("backend_session_id_start"),
                    turn.get("backend_session_id_end"),
                    turn["requested_model"],
                    _json(turn["observed_models"]),
                    turn["started_at"],
                    turn["ended_at"],
                    turn["final_status"],
                    turn["timeout_status"],
                    turn["final_exit_code"],
                    turn["final_invocation_id"],
                    _json(turn["metrics"]),
                    _json(turn["coverage"]),
                    _json(turn["data_quality"]),
                    turn["projection_version"],
                    now,
                    now,
                ),
            )

            # Projections are disposable. Rebuild child rows from the append-only
            # event set so late events and adapter fixes cannot leave stale counts.
            conn.execute("DELETE FROM llm_model_requests WHERE turn_id = ?", (turn_id,))
            conn.execute(
                """
                DELETE FROM llm_invocation_processes
                WHERE invocation_id IN (
                    SELECT invocation_id FROM llm_invocations WHERE turn_id = ?
                )
                """,
                (turn_id,),
            )
            conn.execute("DELETE FROM llm_invocations WHERE turn_id = ?", (turn_id,))

            for process in projection["processes"]:
                conn.execute(
                    """
                    INSERT INTO llm_processes (
                        process_instance_id, node_id, pid, parent_process_instance_id,
                        process_role, backend, executable_name, started_at, ended_at,
                        exit_code, signal, status, data_quality_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(process_instance_id) DO UPDATE SET
                        node_id = excluded.node_id,
                        pid = excluded.pid,
                        parent_process_instance_id = excluded.parent_process_instance_id,
                        process_role = excluded.process_role,
                        backend = excluded.backend,
                        executable_name = excluded.executable_name,
                        started_at = excluded.started_at,
                        ended_at = excluded.ended_at,
                        exit_code = excluded.exit_code,
                        signal = excluded.signal,
                        status = excluded.status,
                        data_quality_json = excluded.data_quality_json
                    """,
                    (
                        process["process_instance_id"],
                        process["node_id"],
                        process["pid"],
                        process["parent_process_instance_id"],
                        process["process_role"],
                        process["backend"],
                        process["executable_name"],
                        process["started_at"],
                        process["ended_at"],
                        process["exit_code"],
                        process["signal"],
                        process["status"],
                        _json(process["data_quality"]),
                    ),
                )

            for invocation in projection["invocations"]:
                conn.execute(
                    """
                    INSERT INTO llm_invocations (
                        invocation_id, turn_id, parent_invocation_id,
                        retry_of_invocation_id, duplicate_of_invocation_id, attempt,
                        spawn_reason, action, node_id, backend, requested_model,
                        observed_model, process_instance_id, pid, process_started_at,
                        started_at, ended_at, status, timeout_kind, exit_code, signal,
                        retry_reason, model_request_count, tool_call_count,
                        subagent_count, usage_json, coverage_json, data_quality_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        invocation["invocation_id"],
                        invocation["turn_id"],
                        invocation["parent_invocation_id"],
                        invocation["retry_of_invocation_id"],
                        invocation["duplicate_of_invocation_id"],
                        invocation["attempt"],
                        invocation["spawn_reason"],
                        invocation["action"],
                        invocation["node_id"],
                        invocation["backend"],
                        invocation["requested_model"],
                        invocation["observed_model"],
                        invocation["process_instance_id"],
                        invocation["pid"],
                        invocation["process_started_at"],
                        invocation["started_at"],
                        invocation["ended_at"],
                        invocation["status"],
                        invocation["timeout_kind"],
                        invocation["exit_code"],
                        invocation["signal"],
                        invocation["retry_reason"],
                        invocation["model_request_count"],
                        invocation["tool_call_count"],
                        invocation["subagent_count"],
                        _json(invocation["usage"]),
                        _json(invocation["coverage"]),
                        _json(invocation["data_quality"]),
                    ),
                )

            for link in projection["process_links"]:
                conn.execute(
                    """
                    INSERT INTO llm_invocation_processes (
                        invocation_id, process_instance_id, relationship
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        link["invocation_id"],
                        link["process_instance_id"],
                        link["relationship"],
                    ),
                )

            for request in projection["model_requests"]:
                conn.execute(
                    """
                    INSERT INTO llm_model_requests (
                        model_request_id, invocation_id, turn_id, sequence,
                        provider_request_id, model, work_category, started_at,
                        ended_at, status, input_tokens, output_tokens,
                        cache_read_tokens, cache_creation_tokens, reasoning_tokens,
                        context_tokens, input_token_semantics, usage_granularity,
                        usage_source, usage_coverage, is_duplicate, data_quality_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request["model_request_id"],
                        request["invocation_id"],
                        request["turn_id"],
                        request["sequence"],
                        request["provider_request_id"],
                        request["model"],
                        request["work_category"],
                        request["started_at"],
                        request["ended_at"],
                        request["status"],
                        request["input_tokens"],
                        request["output_tokens"],
                        request["cache_read_tokens"],
                        request["cache_creation_tokens"],
                        request["reasoning_tokens"],
                        request["context_tokens"],
                        request["input_token_semantics"],
                        request["usage_granularity"],
                        request["usage_source"],
                        request["usage_coverage"],
                        1 if request["is_duplicate"] else 0,
                        _json(request["data_quality"]),
                    ),
                )
        return projection

    def _all_events(self, turn_id: str) -> List[Dict[str, Any]]:
        """Internal unbounded read used for accounting; public APIs remain paged."""
        rows = self.db._conn().execute(
            """
            SELECT * FROM llm_events
            WHERE turn_id = ?
            ORDER BY event_time, source, COALESCE(source_sequence, 0), event_id
            """,
            (turn_id,),
        ).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["attributes"] = json.loads(item.get("attributes") or "{}")
            except Exception:
                item["attributes"] = {}
            result.append(item)
        return result

    def get_turn(self, turn_id: str) -> Optional[Dict[str, Any]]:
        row = self.db._conn().execute(
            "SELECT * FROM llm_turns WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        if not row:
            return None
        return self._decode_turn(dict(row))

    def list_turns(
        self,
        *,
        session_id: Optional[str] = None,
        status: Optional[str] = None,
        backend: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        where: List[str] = []
        params: List[Any] = []
        for column, value in (
            ("session_id", session_id),
            ("final_status", status),
            ("backend", backend),
        ):
            if value:
                where.append(f"{column} = ?")
                params.append(value)
        sql = "SELECT * FROM llm_turns"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(started_at, created_at) DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        rows = self.db._conn().execute(sql, params).fetchall()
        return [self._decode_turn(dict(row)) for row in rows]

    def get_invocations(self, turn_id: str) -> List[Dict[str, Any]]:
        rows = self.db._conn().execute(
            """
            SELECT * FROM llm_invocations
            WHERE turn_id = ?
            ORDER BY attempt, COALESCE(started_at, ''), invocation_id
            """,
            (turn_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key, default in (
                ("usage_json", {}),
                ("coverage_json", {}),
                ("data_quality_json", []),
            ):
                try:
                    item[key] = json.loads(item.get(key) or _json(default))
                except Exception:
                    item[key] = default
            item["usage"] = item.pop("usage_json")
            item["coverage"] = item.pop("coverage_json")
            item["data_quality"] = item.pop("data_quality_json")
            result.append(item)
        return result

    def get_model_requests(self, turn_id: str) -> List[Dict[str, Any]]:
        rows = self.db._conn().execute(
            """
            SELECT * FROM llm_model_requests
            WHERE turn_id = ?
            ORDER BY invocation_id, sequence, model_request_id
            """,
            (turn_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["data_quality"] = json.loads(item.pop("data_quality_json") or "[]")
            except Exception:
                item["data_quality"] = []
            item["is_duplicate"] = bool(item["is_duplicate"])
            result.append(item)
        return result

    def get_processes(self, turn_id: str) -> List[Dict[str, Any]]:
        rows = self.db._conn().execute(
            """
            SELECT p.*, ip.invocation_id, ip.relationship
            FROM llm_processes p
            JOIN llm_invocation_processes ip
              ON ip.process_instance_id = p.process_instance_id
            JOIN llm_invocations i ON i.invocation_id = ip.invocation_id
            WHERE i.turn_id = ?
            ORDER BY COALESCE(p.started_at, ''), p.process_instance_id
            """,
            (turn_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["data_quality"] = json.loads(item.pop("data_quality_json") or "[]")
            except Exception:
                item["data_quality"] = []
            result.append(item)
        return result

    def diagnostics(self, turn_id: str) -> Optional[Dict[str, Any]]:
        turn = self.get_turn(turn_id)
        if turn is None:
            return None
        return {
            "turn": turn,
            "invocations": self.get_invocations(turn_id),
            "model_requests": self.get_model_requests(turn_id),
            "processes": self.get_processes(turn_id),
        }

    def graph(self, turn_id: str, *, expand_tools: bool = False) -> Optional[Dict[str, Any]]:
        diagnostics = self.diagnostics(turn_id)
        if diagnostics is None:
            return None
        turn = diagnostics["turn"]
        nodes: List[Dict[str, Any]] = [
            {
                "id": f"turn:{turn_id}",
                "kind": "turn",
                "label": turn_id,
                "status": turn["final_status"],
                "metrics": turn["metrics"],
            }
        ]
        edges: List[Dict[str, str]] = []

        for invocation in diagnostics["invocations"]:
            invocation_id = invocation["invocation_id"]
            nodes.append(
                {
                    "id": invocation_id,
                    "kind": "invocation",
                    "label": (
                        f"{invocation['backend']} attempt {invocation['attempt']}"
                    ),
                    "status": invocation["status"],
                    "started_at": invocation["started_at"],
                    "ended_at": invocation["ended_at"],
                    "metrics": invocation["usage"],
                }
            )
            edges.append(
                {"from": f"turn:{turn_id}", "to": invocation_id, "kind": "contains"}
            )
            if invocation["retry_of_invocation_id"]:
                edges.append(
                    {
                        "from": invocation["retry_of_invocation_id"],
                        "to": invocation_id,
                        "kind": "retry",
                    }
                )

        for process in diagnostics["processes"]:
            process_id = f"process:{process['process_instance_id']}"
            nodes.append(
                {
                    "id": process_id,
                    "kind": "process",
                    "label": f"{process['executable_name'] or 'process'} pid={process['pid']}",
                    "status": process["status"],
                    "started_at": process["started_at"],
                    "ended_at": process["ended_at"],
                    "metrics": {"exit_code": process["exit_code"]},
                }
            )
            edges.append(
                {
                    "from": process["invocation_id"],
                    "to": process_id,
                    "kind": process["relationship"],
                }
            )

        for request in diagnostics["model_requests"]:
            request_id = f"model:{request['model_request_id']}"
            nodes.append(
                {
                    "id": request_id,
                    "kind": (
                        "model_request"
                        if request["usage_granularity"] == "request"
                        else "aggregate_usage"
                    ),
                    "label": request["model"] or request["usage_granularity"],
                    "status": request["status"] or "observed",
                    "started_at": request["started_at"],
                    "ended_at": request["ended_at"],
                    "metrics": {
                        "input_tokens": request["input_tokens"],
                        "output_tokens": request["output_tokens"],
                        "cache_read_tokens": request["cache_read_tokens"],
                        "context_tokens": request["context_tokens"],
                    },
                }
            )
            edges.append(
                {
                    "from": request["invocation_id"],
                    "to": request_id,
                    "kind": "model_work",
                }
            )

        tool_events = [
            event
            for event in self.list_events(turn_id, limit=5000)
            if event["event_name"].startswith("tool.call.")
        ]
        completed_tools: Dict[str, Dict[str, Any]] = {}
        for event in tool_events:
            if event.get("tool_call_id"):
                completed_tools[event["tool_call_id"]] = event
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for event in completed_tools.values():
            grouped.setdefault(event.get("invocation_id") or "", []).append(event)
        for invocation_id, tools in grouped.items():
            if expand_tools and len(nodes) + len(tools) <= 500:
                for tool in tools:
                    node_id = f"tool:{tool['tool_call_id']}"
                    attrs = tool["attributes"]
                    nodes.append(
                        {
                            "id": node_id,
                            "kind": "tool_call",
                            "label": attrs.get("tool_name") or "tool",
                            "status": attrs.get("status") or "observed",
                            "started_at": None,
                            "ended_at": tool["event_time"],
                            "metrics": {"category": attrs.get("tool_category")},
                        }
                    )
                    edges.append(
                        {"from": invocation_id, "to": node_id, "kind": "tool"}
                    )
            else:
                node_id = f"tools:{invocation_id}"
                nodes.append(
                    {
                        "id": node_id,
                        "kind": "tool_group",
                        "label": f"{len(tools)} tool calls",
                        "status": "observed",
                        "metrics": {"tool_call_count": len(tools)},
                    }
                )
                edges.append({"from": invocation_id, "to": node_id, "kind": "tools"})

        return {
            "turn_id": turn_id,
            "nodes": nodes,
            "edges": edges,
            "coverage": turn["coverage"],
            "data_quality": turn["data_quality"],
        }

    @staticmethod
    def _decode_turn(row: Dict[str, Any]) -> Dict[str, Any]:
        for key, default in (
            ("observed_models", []),
            ("metrics_json", {}),
            ("coverage_json", {}),
            ("data_quality_json", []),
        ):
            try:
                row[key] = json.loads(row.get(key) or _json(default))
            except Exception:
                row[key] = default
        row["metrics"] = row.pop("metrics_json")
        row["coverage"] = row.pop("coverage_json")
        row["data_quality"] = row.pop("data_quality_json")
        return row
