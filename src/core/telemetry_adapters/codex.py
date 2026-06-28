"""Codex CLI 0.140 JSONL telemetry adapter.

Only fields proven by repository artifacts/fixtures are mapped.  Backend event
objects are never retained.  In particular command text, file changes, search
queries, aggregated output, and agent messages are discarded.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.core.telemetry import TelemetryContext, TelemetryEvent, build_event

ADAPTER_VERSION = "codex-jsonl-0.140-v1"

_TOOL_TYPES: Dict[str, tuple[str, str]] = {
    "command_execution": ("command_execution", "shell"),
    "file_change": ("file_change", "file_write"),
    "web_search": ("web_search", "search"),
}


class CodexTelemetryAdapter:
    """Stateful line adapter scoped to one Codex invocation."""

    def __init__(
        self,
        context: TelemetryContext,
        *,
        emitter_process_instance_id: str,
    ) -> None:
        self.context = context
        self.emitter_process_instance_id = emitter_process_instance_id
        self._sequence = 0
        self._tool_sequence = 0

    def consume_line(
        self,
        line: str,
        *,
        event_time: Optional[datetime] = None,
    ) -> List[TelemetryEvent]:
        raw = (line or "").strip()
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return [
                self._event(
                    "telemetry.parse_error",
                    event_time=event_time,
                    attributes={
                        "backend_event_type": "non_json",
                        "error_code": "invalid_json",
                        "adapter_version": ADAPTER_VERSION,
                    },
                )
            ]
        if not isinstance(payload, dict):
            return []

        event_type = str(payload.get("type") or "")
        if event_type in ("item.started", "item.completed"):
            return self._consume_item(payload, event_time=event_time)
        if event_type == "turn.completed":
            return self._consume_usage(payload, event_time=event_time)
        if event_type == "turn.failed":
            return [
                self._event(
                    "telemetry.coverage",
                    event_time=event_time,
                    attributes={
                        "area": "usage",
                        "coverage": "partial",
                        "reason_code": "turn_failed_without_usage",
                        "adapter_version": ADAPTER_VERSION,
                    },
                )
            ]
        return []

    def coverage_events(self, *, event_time: Optional[datetime] = None) -> List[TelemetryEvent]:
        return [
            self._event(
                "telemetry.coverage",
                event_time=event_time,
                attributes={
                    "area": "usage",
                    "coverage": "aggregate_only",
                    "reason_code": "codex_turn_total_only",
                    "adapter_version": ADAPTER_VERSION,
                },
            ),
            self._event(
                "telemetry.coverage",
                event_time=event_time,
                attributes={
                    "area": "tools",
                    "coverage": "complete",
                    "reason_code": "codex_item_events",
                    "adapter_version": ADAPTER_VERSION,
                },
            ),
            self._event(
                "telemetry.coverage",
                event_time=event_time,
                attributes={
                    "area": "subagents",
                    "coverage": "unsupported",
                    "reason_code": "no_supported_subagent_event",
                    "adapter_version": ADAPTER_VERSION,
                },
            ),
        ]

    def _consume_item(
        self, payload: Dict[str, Any], *, event_time: Optional[datetime]
    ) -> List[TelemetryEvent]:
        item = payload.get("item")
        if not isinstance(item, dict):
            return []
        item_type = str(item.get("type") or "")
        mapping = self._tool_mapping(item_type, item)
        if mapping is None:
            return []
        tool_name, category = mapping
        item_id = str(item.get("id") or "")
        if not item_id:
            return [
                self._event(
                    "telemetry.parse_error",
                    event_time=event_time,
                    attributes={
                        "backend_event_type": str(payload.get("type") or "item"),
                        "error_code": "tool_item_id_missing",
                        "adapter_version": ADAPTER_VERSION,
                    },
                )
            ]

        self._tool_sequence += 1
        lifecycle = "started" if payload.get("type") == "item.started" else "completed"
        attributes: Dict[str, Any] = {
            "tool_name": tool_name,
            "tool_category": category,
            "sequence": self._tool_sequence,
        }
        if lifecycle == "completed":
            attributes["status"] = self._tool_status(item)
        return [
            self._event(
                f"tool.call.{lifecycle}",
                event_time=event_time,
                tool_call_id=item_id,
                attributes=attributes,
            )
        ]

    def _consume_usage(
        self, payload: Dict[str, Any], *, event_time: Optional[datetime]
    ) -> List[TelemetryEvent]:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return [
                self._event(
                    "telemetry.coverage",
                    event_time=event_time,
                    attributes={
                        "area": "usage",
                        "coverage": "partial",
                        "reason_code": "turn_completed_usage_missing",
                        "adapter_version": ADAPTER_VERSION,
                    },
                )
            ]

        attributes: Dict[str, Any] = {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_tokens": usage.get("cached_input_tokens"),
            "reasoning_tokens": usage.get("reasoning_output_tokens"),
            # Codex reports cached_input_tokens as a subset of input_tokens.
            "input_token_semantics": "includes_cache",
            "usage_granularity": "invocation_total",
            "usage_source": "turn.completed.usage",
            "usage_coverage": "aggregate_only",
            "counter_semantics": "final_invocation_total",
            "work_category": (
                "retry" if self.context.spawn_reason == "retry" else "primary"
            ),
        }
        # Omit absent optional counters rather than turning unknown into zero.
        attributes = {key: value for key, value in attributes.items() if value is not None}
        return [
            self._event(
                "model.request.usage",
                event_time=event_time,
                attributes=attributes,
            )
        ]

    @staticmethod
    def _tool_status(item: Dict[str, Any]) -> str:
        status = str(item.get("status") or "").lower()
        if status in ("completed", "success", "succeeded"):
            return "success"
        if status in ("failed", "error"):
            return "failed"
        return "unknown"

    def _event(
        self,
        event_name: str,
        *,
        event_time: Optional[datetime],
        attributes: Dict[str, Any],
        tool_call_id: Optional[str] = None,
    ) -> TelemetryEvent:
        self._sequence += 1
        return build_event(
            event_name,
            turn_id=self.context.turn_id,
            session_id=self.context.session_id,
            node_id=self.context.node_id,
            emitter_process_instance_id=self.emitter_process_instance_id,
            source="backend",
            source_sequence=self._sequence,
            invocation_id=self.context.invocation_id,
            tool_call_id=tool_call_id,
            backend=self.context.backend or "codex",
            model=self.context.model,
            event_time=event_time,
            attributes=attributes,
        )

    @staticmethod
    def _tool_mapping(item_type: str, item: Dict[str, Any]) -> Optional[tuple[str, str]]:
        if item_type == "mcp_tool_call":
            server = str(item.get("server") or "").strip()
            tool = str(item.get("tool") or "").strip()
            if server and tool:
                return (f"{server}.{tool}", "mcp")
            if tool:
                return (tool, "mcp")
            return ("mcp_tool_call", "mcp")
        return _TOOL_TYPES.get(item_type)
