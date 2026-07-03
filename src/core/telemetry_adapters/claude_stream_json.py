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

**Token semantics (Claude API):**  ``input_tokens`` already *includes*
``cache_read_input_tokens`` (inclusive-cache semantics).  Therefore:
    context_tokens = input_tokens          (NOT input_tokens + cache_read_tokens)
This is the same semantics Codex uses; see spec §5.2.

**Double-count guard:**  Claude emits usage in the last ``type=assistant``
message AND in the final ``type=result`` message.  With
``--include-partial-messages`` OMITTED (which this project does), only one
assistant message carries usage.  The adapter emits exactly ONE
``model.request.usage`` event per invocation: if both ``type=assistant`` and
``type=result`` carry usage, the ``type=result`` usage wins (it is the
authoritative final aggregate).  A guard prevents emitting the assistant usage
if result usage is seen later.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.core.telemetry import TelemetryContext, TelemetryEvent, build_event

ADAPTER_VERSION = "claude-stream-json-v1"

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
            events = [self._build_usage_event(
                self._pending_assistant_usage,
                event_time=event_time,
                usage_source="claude.assistant.message.usage",
            )]
            self._pending_assistant_usage = None
            return events
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
        message = payload.get("message")
        if isinstance(message, dict):
            usage = message.get("usage")
            if isinstance(usage, dict) and usage:
                # Hold it: a type=result usage (the authoritative aggregate) may
                # follow and should supersede this one.
                self._pending_assistant_usage = usage
        # Never store message content (text blocks, tool_use arrays, etc.)
        return []

    def _consume_result(
        self,
        payload: Dict[str, Any],
        *,
        event_time: Optional[datetime],
    ) -> List[TelemetryEvent]:
        """Handle ``type=result``.  Emit usage from here; discard assistant pending."""
        events: List[TelemetryEvent] = []
        usage = payload.get("usage")
        if isinstance(usage, dict) and usage:
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

        Claude inclusive-cache semantics (verified by parse_cache_stats_from_ndjson):
          - ``input_tokens`` already includes ``cache_read_input_tokens``.
          - context_tokens = input_tokens  (spec §5.2 inclusive-cache normalisation).
          - Do NOT add cache_read_tokens to input_tokens.
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
            # context_tokens = input_tokens under inclusive-cache semantics.
            "context_tokens": input_tokens,
            "input_token_semantics": "includes_cache",
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
            model=self.context.model,
            event_time=event_time,
            attributes=attributes,
        )


def _int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        v = int(value)
        return v if v >= 0 else None  # reject negative token counts
    except (TypeError, ValueError):
        return None
