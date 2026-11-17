"""Claude CLI stream-json telemetry adapter (M3).

Maps Claude's ``--output-format stream-json`` NDJSON output to the typed
TelemetryEvent contract.  The adapter is stateful and scoped to one invocation.

**NDJSON shape (SDK-synthesised or direct CLI):**

    {"type": "assistant", "message": {"usage": {
        "input_tokens": N,
        "output_tokens": N,
        "cache_read_input_tokens": N,
        "cache_creation_input_tokens": N}}}
    {"type": "result",    "usage": {...}, "result": "<SANITIZED>"}
    {"type": "tool_use",  "id": "toolu_01", "name": "Bash",
                          "input": {"command": "<SECRET>"}}
    {"type": "tool_result", "tool_use_id": "toolu_01",
                            "content": "<SECRET>"}

**Privacy rule (default-deny):**  field values are never stored unless they
appear in the EVENT_ATTRIBUTE_ALLOWLIST.  ``input``, ``command``, ``content``,
``result``, ``text``, ``arguments`` are NEVER stored.  Only structural keys
(token counts, IDs, category codes) pass through.

**Token semantics:** Claude input excludes cache reads and writes. Request context
is their sum; invocation totals are spend counters, never a context measurement.

**Double-count guard:** post-processing emits one final invocation aggregate.
The SDK's live assistant snapshots are deduplicated by message identity and
replace the SAME aggregate row; final ``type=result`` usage supersedes them.
Unidentified messages cannot provide deduplicated live totals and wait for the
final aggregate. A truncated identified stream retains the known message totals.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.core.telemetry import TelemetryContext, TelemetryEvent, build_event

ADAPTER_VERSION = "claude-stream-json-v2"

# Sanitised tool name: only safe identifier characters, max 80 chars.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.:\-]")
_MAX_TOOL_NAME = 80

# Tool-name → category mapping (Claude-native names).
_TOOL_CATEGORIES: Dict[str, str] = {
    "Bash": "shell",
    "Edit": "file_write",
    "MultiEdit": "file_write",
    "Read": "file_read",
    "Write": "file_write",
    "LS": "file_read",
    "Grep": "search",
    "Glob": "search",
    "WebSearch": "search",
    "WebFetch": "search",
    "TodoRead": "file_read",
    "TodoWrite": "file_write",
}


def _sanitise_tool_name(raw: str) -> str:
    clean = _SAFE_NAME_RE.sub("_", (raw or "").strip())[:_MAX_TOOL_NAME]
    return clean or "unknown"


def _tool_category(name: str) -> str:
    return _TOOL_CATEGORIES.get(name, "other")


class ClaudeStreamJsonAdapter:
    """Stateful line adapter scoped to one Claude invocation.

    Produces TelemetryEvent objects from Claude's stream-json NDJSON.
    No raw content is retained: usage counters, tool category/name, and
    status codes only.
    """

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
        # Pending assistant usage that may be superseded by type=result usage.
        self._pending_assistant_usage: Optional[Dict[str, Any]] = None
        # Set to True once a type=result usage event is emitted so we know to
        # discard the pending assistant usage.
        self._result_usage_emitted = False
        # Bounded per-message snapshots; repeated SDK frames replace counters.
        self._live_messages: dict[str, dict[str, int]] = {}
        self._live_totals: dict[str, int] = {}
        self._live_last_context: int | None = None
        self._live_first_context: int | None = None
        self._context_window: int | None = None
        self._observed_model: str | None = None
        self._reported_cost: float | None = None
        self._compaction_before: int | None = None
        self._compaction_trigger: str = "unknown"

    def observe_assistant_usage(
        self, usage: dict[str, object], message_id: str,
    ) -> list[TelemetryEvent]:
        """Emit an interim invocation aggregate under the final aggregate's ID.

        Missing identities cannot be deduplicated, so wait for final usage.
        Memory is bounded to 4096 messages × four integer counters; overflow
        stops interim updates, while final result processing remains intact.
        """
        if not isinstance(message_id, str) or not message_id or len(message_id) > 512 or self._result_usage_emitted:
            return []
        fields = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        if any(field in usage and _int_or_none(usage[field]) is None for field in fields):
            self._live_last_context = None
            return []
        counters: dict[str, int] = {
            field: value for field in fields
            if type(value := usage.get(field)) is int and 0 <= value <= 2**63 - 1
        }
        if not counters:
            return []
        if message_id not in self._live_messages and len(self._live_messages) >= 4096:
            self._live_last_context = None
            return []
        previous = self._live_messages.get(message_id, {})
        updated = {**previous, **counters}
        if updated == previous:
            return []
        if any(self._live_totals.get(field, 0) + value - previous.get(field, 0) > 2**63 - 1
               for field, value in updated.items()):
            return []
        self._live_messages[message_id] = updated
        self._live_last_context = (
            sum(updated.get(field, 0) for field in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            if "input_tokens" in updated else None
        )
        if self._live_first_context is None:
            self._live_first_context = self._live_last_context
        for field, value in updated.items():
            self._live_totals[field] = self._live_totals.get(field, 0) + value - previous.get(field, 0)
        event = self._build_usage_event(
            self._live_totals, event_time=None, usage_source="claude.assistant.live",
        )
        events = [event.model_copy(update={"attributes": {
            **event.attributes, "usage_coverage": "partial",
            "counter_semantics": "running_invocation_total",
            # Context describes the latest request, not the sum of requests.
            "context_tokens": self._live_last_context,
        }})]
        if self._compaction_before is not None and self._live_last_context is not None:
            events.append(self._event("context.compacted", event_time=None, attributes={
                "before_tokens": self._compaction_before, "after_tokens": self._live_last_context,
                "trigger": self._compaction_trigger,
            }))
            self._compaction_before = None
        return events

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

        if event_type == "assistant":
            return self._consume_assistant(payload, event_time=event_time)
        if event_type == "result":
            return self._consume_result(payload, event_time=event_time)
        if event_type == "tool_use":
            return self._consume_tool_use(payload, event_time=event_time)
        if event_type == "tool_result":
            # tool_result carries content (user/tool-produced text) — never store.
            return self._consume_tool_result(payload, event_time=event_time)
        if event_type == "system" and payload.get("subtype") == "compact_boundary":
            metadata = payload.get("compact_metadata") or payload.get("compactMetadata")
            if isinstance(metadata, dict):
                self._compaction_before = _int_or_none(metadata.get("pre_tokens") or metadata.get("preTokens"))
                trigger = metadata.get("trigger")
                self._compaction_trigger = trigger if trigger in ("auto", "manual") else "unknown"
            return []
        if event_type in ("system", "user"):
            # Structural scaffolding — no telemetry value, no sensitive content.
            return []

        # Unknown type — emit a coverage marker so the dashboard can show it
        # as unsupported rather than silently dropping it.
        return [
            self._event(
                "telemetry.coverage",
                event_time=event_time,
                attributes={
                    "area": "event_type",
                    "coverage": "unsupported",
                    "reason_code": "unknown_stream_json_type",
                    "adapter_version": ADAPTER_VERSION,
                },
            )
        ]

    def coverage_events(self, *, event_time: Optional[datetime] = None) -> List[TelemetryEvent]:
        """Emit static coverage declarations for this adapter's capabilities."""
        return [
            self._event(
                "telemetry.coverage",
                event_time=event_time,
                attributes={
                    "area": "usage",
                    "coverage": "aggregate_only",
                    "reason_code": "claude_stream_json_invocation_total",
                    "adapter_version": ADAPTER_VERSION,
                },
            ),
            self._event(
                "telemetry.coverage",
                event_time=event_time,
                attributes={
                    "area": "tools",
                    "coverage": "complete",
                    "reason_code": "claude_stream_json_tool_use_events",
                    "adapter_version": ADAPTER_VERSION,
                },
            ),
            self._event(
                "telemetry.coverage",
                event_time=event_time,
                attributes={
                    "area": "subagents",
                    "coverage": "unsupported",
                    "reason_code": "no_subagent_event_in_stream_json",
                    "adapter_version": ADAPTER_VERSION,
                },
            ),
            self._event(
                "telemetry.coverage",
                event_time=event_time,
                attributes={
                    "area": "hook_integration",
                    "coverage": "unsupported",
                    "reason_code": "stream_only_no_hooks",
                    "adapter_version": ADAPTER_VERSION,
                },
            ),
        ]

    def flush_pending_usage(self, *, event_time: Optional[datetime] = None) -> List[TelemetryEvent]:
        """Emit any pending assistant-level usage that was not superseded by result usage.

        Call this after processing all lines to ensure the usage event is emitted
        even when no ``type=result`` line appears (e.g. truncated stream on kill).
        """
        if self._pending_assistant_usage is not None and not self._result_usage_emitted:
            event = self._build_usage_event(
                self._pending_assistant_usage,
                event_time=event_time,
                usage_source="claude.assistant.message.usage",
            )
            if self._live_totals:
                event = event.model_copy(update={"attributes": {
                    **event.attributes, "context_tokens": self._live_last_context,
                    "usage_coverage": "partial",
                }})
            self._pending_assistant_usage = None
            return [event]
        return []

    # ------------------------------------------------------------------
    # Internal event consumers
    # ------------------------------------------------------------------

    def _consume_assistant(
        self,
        payload: Dict[str, Any],
        *,
        event_time: Optional[datetime],
    ) -> List[TelemetryEvent]:
        """Handle ``type=assistant``.  Store usage as pending; never emit yet."""
        events: List[TelemetryEvent] = []
        message = payload.get("message")
        if isinstance(message, dict):
            model = message.get("model")
            if isinstance(model, str) and 0 < len(model) <= 128:
                self._observed_model = model
            usage = message.get("usage")
            if isinstance(usage, dict) and usage:
                # Hold it: a type=result usage (the authoritative aggregate) may
                # follow and should supersede this one.
                message_id = message.get("id")
                if isinstance(message_id, str) and message_id:
                    events.extend(event for event in self.observe_assistant_usage(usage, message_id)
                                  if event.event_name == "context.compacted")
                    self._pending_assistant_usage = dict(self._live_totals) or None
                else:
                    self._pending_assistant_usage = usage
        # Never store message content (text blocks, tool_use arrays, etc.)
        return events

    def _consume_result(
        self,
        payload: Dict[str, Any],
        *,
        event_time: Optional[datetime],
    ) -> List[TelemetryEvent]:
        """Handle ``type=result``.  Emit usage from here; discard assistant pending."""
        events: List[TelemetryEvent] = []
        reported_cost = payload.get("total_cost_usd")
        if type(reported_cost) in (int, float) and math.isfinite(reported_cost) and reported_cost >= 0:
            self._reported_cost = float(reported_cost)
        models = payload.get("modelUsage")
        if isinstance(models, dict):
            selected = models.get(self._observed_model or self.context.model)
            if not isinstance(selected, dict) and len(models) == 1:
                observed_model, selected = next(iter(models.items()))
                if isinstance(observed_model, str) and 0 < len(observed_model) <= 128:
                    self._observed_model = observed_model
            if isinstance(selected, dict):
                self._context_window = _int_or_none(selected.get("contextWindow"))
        usage = payload.get("usage")
        if (isinstance(usage, dict) and usage) or self._reported_cost is not None:
            usage = usage if isinstance(usage, dict) else {}
            # This is the authoritative final aggregate; discard any pending
            # assistant usage to avoid double-counting.
            self._pending_assistant_usage = None
            self._result_usage_emitted = True
            events.append(self._build_usage_event(
                usage,
                event_time=event_time,
                usage_source="claude.result.usage",
            ))

        # ``stop_reason`` and ``is_error`` are structural — safe to record.
        stop_reason = str(payload.get("stop_reason") or "").strip() or None
        is_error = bool(payload.get("is_error", False))
        status = "failed" if is_error else "success"

        events.append(self._event(
            "invocation.completed",
            event_time=event_time,
            attributes={
                "status": status,
                **({"error_code": "backend_error"} if is_error else {}),
                **({"exit_code": 1} if is_error else {"exit_code": 0}),
            },
        ))
        return events

    def _consume_tool_use(
        self,
        payload: Dict[str, Any],
        *,
        event_time: Optional[datetime],
    ) -> List[TelemetryEvent]:
        """Handle ``type=tool_use`` — emit tool.call.started; NEVER store input."""
        tool_id = str(payload.get("id") or "").strip()
        raw_name = str(payload.get("name") or "").strip()
        tool_name = _sanitise_tool_name(raw_name)
        category = _tool_category(raw_name)
        # Never store payload.get("input") — it contains tool arguments.
        self._tool_sequence += 1
        return [
            self._event(
                "tool.call.started",
                event_time=event_time,
                tool_call_id=tool_id or None,
                attributes={
                    "tool_name": tool_name,
                    "tool_category": category,
                    "sequence": self._tool_sequence,
                },
            )
        ]

    def _consume_tool_result(
        self,
        payload: Dict[str, Any],
        *,
        event_time: Optional[datetime],
    ) -> List[TelemetryEvent]:
        """Handle ``type=tool_result`` — emit tool.call.completed; NEVER store content."""
        tool_id = str(payload.get("tool_use_id") or "").strip()
        # Never store payload.get("content") — it is tool result text.
        # We do not know the duration here (no start timestamp), so omit it.
        return [
            self._event(
                "tool.call.completed",
                event_time=event_time,
                tool_call_id=tool_id or None,
                attributes={
                    "tool_name": "unknown",   # name not repeated in tool_result
                    "tool_category": "other",
                    "sequence": self._tool_sequence,
                    "status": "success",
                },
            )
        ]

    def _build_usage_event(
        self,
        usage: Dict[str, Any],
        *,
        event_time: Optional[datetime],
        usage_source: str,
    ) -> TelemetryEvent:
        """Build a model.request.usage event from a Claude usage dict.

        Spend comes from aggregate counters; context comes from assistant messages.
        """
        input_tokens = _int_or_none(usage.get("input_tokens"))
        output_tokens = _int_or_none(usage.get("output_tokens"))
        cache_read = _int_or_none(
            usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens")
        )
        cache_create = _int_or_none(
            usage.get("cache_creation_input_tokens") or usage.get("cache_creation_tokens")
        )

        attributes: Dict[str, Any] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": cache_create,
            "context_tokens": self._live_last_context if usage_source == "claude.result.usage" or self._live_totals else (
                input_tokens + (cache_read or 0) + (cache_create or 0)
                if input_tokens is not None else None
            ),
            "context_entry_tokens": self._live_first_context,
            "context_window_tokens": self._context_window,
            "adapter_version": ADAPTER_VERSION,
            "provider_reported_cost_usd": self._reported_cost,
            "input_token_semantics": "excludes_cache",
            "usage_granularity": "invocation_total",
            "usage_source": usage_source,
            "usage_coverage": "aggregate_only",
            "counter_semantics": "final_invocation_total",
            "work_category": (
                "retry" if self.context.spawn_reason == "retry" else "primary"
            ),
        }
        # Omit None values rather than emitting null for unknown counts.
        attributes = {k: v for k, v in attributes.items() if v is not None}
        return self._event(
            "model.request.usage",
            event_time=event_time,
            attributes=attributes,
        )

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
            backend=self.context.backend or "claude",
            model=self._observed_model or self.context.model,
            event_time=event_time,
            attributes=attributes,
        )


def _int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None
