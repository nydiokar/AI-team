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

    def consume_token_count(
        self,
        payload: Dict[str, Any],
        *,
        event_time: Optional[datetime] = None,
    ) -> List[TelemetryEvent]:
        """Map Codex rollout token_count events without retaining transcript data."""
        info = payload.get("info")
        if not isinstance(info, dict):
            return []
        last_usage = info.get("last_token_usage")
        total_usage = info.get("total_token_usage")
        context_window = info.get("model_context_window")
        events: List[TelemetryEvent] = []
        if isinstance(last_usage, dict):
            request_attrs: Dict[str, Any] = {
                "sequence": self._sequence + 1,
                "input_tokens": last_usage.get("input_tokens"),
                "output_tokens": last_usage.get("output_tokens"),
                "cache_read_tokens": last_usage.get("cached_input_tokens"),
                "reasoning_tokens": last_usage.get("reasoning_output_tokens"),
                "context_window_tokens": context_window,
                "input_token_semantics": "includes_cache",
                "usage_granularity": "request",
                "usage_source": "codex.rollout.token_count.last_token_usage",
                "usage_coverage": "complete",
                "counter_semantics": "per_request",
                "work_category": (
                    "retry" if self.context.spawn_reason == "retry" else "primary"
                ),
            }
            request_attrs = {
                key: value for key, value in request_attrs.items() if value is not None
            }
            events.append(
                self._event(
                    "model.request.usage",
                    event_time=event_time,
                    model_request_id=f"{self.context.invocation_id}:request:{self._sequence + 1}",
                    attributes=request_attrs,
                )
            )
        if isinstance(total_usage, dict):
            session_attrs: Dict[str, Any] = {
                "input_tokens": total_usage.get("input_tokens"),
                "output_tokens": total_usage.get("output_tokens"),
                "cache_read_tokens": total_usage.get("cached_input_tokens"),
                "reasoning_tokens": total_usage.get("reasoning_output_tokens"),
                "total_tokens": total_usage.get("total_tokens"),
                "context_window_tokens": context_window,
                "usage_source": "codex.rollout.token_count.total_token_usage",
                "counter_semantics": "session_cumulative_total",
            }
            rate_limits = payload.get("rate_limits")
            if isinstance(rate_limits, dict):
                primary = rate_limits.get("primary")
                secondary = rate_limits.get("secondary")
                if isinstance(primary, dict):
                    session_attrs.update(
                        {
                            "rate_limit_primary_used_percent": primary.get("used_percent"),
                            "rate_limit_primary_window_minutes": primary.get("window_minutes"),
                            "rate_limit_primary_resets_at": primary.get("resets_at"),
                        }
                    )
                if isinstance(secondary, dict):
                    session_attrs.update(
                        {
                            "rate_limit_secondary_used_percent": secondary.get("used_percent"),
                            "rate_limit_secondary_window_minutes": secondary.get("window_minutes"),
                            "rate_limit_secondary_resets_at": secondary.get("resets_at"),
                        }
                    )
                session_attrs["rate_limit_plan_type"] = rate_limits.get("plan_type")
                session_attrs["rate_limit_reached_type"] = rate_limits.get(
                    "rate_limit_reached_type"
                )
            session_attrs = {
                key: value for key, value in session_attrs.items() if value is not None
            }
            events.append(
                self._event(
                    "model.session_usage",
                    event_time=event_time,
                    attributes=session_attrs,
                )
            )
        return events

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
        model_request_id: Optional[str] = None,
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
            model_request_id=model_request_id,
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
