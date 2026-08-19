"""Experimental Claude Code ``get_usage`` control-request adapter.

This module intentionally does not call Claude's usage endpoint directly and
does not read Claude Code status-line/cache files. It sends the same control
request that the TypeScript Agent SDK uses to the already-connected Claude Code
SDK transport.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class ClaudeScopedLimit(BaseModel):
    kind: str | None = None
    group: str | None = None
    percent: float | None = None
    resets_at: datetime | None = None
    model_id: str | None = None
    model_display_name: str | None = None
    is_active: bool | None = None
    raw: dict[str, object] = Field(default_factory=dict)


class ClaudeQuotaSnapshot(BaseModel):
    status: Literal["valid", "unavailable", "stale"]
    observed_at: datetime
    five_hour_utilization: float | None = None
    five_hour_resets_at: datetime | None = None
    seven_day_utilization: float | None = None
    seven_day_resets_at: datetime | None = None
    scoped_limits: list[ClaudeScopedLimit] = Field(default_factory=list)
    subscription_type: str | None = None
    rate_limits_available: bool | None = None
    sdk_version: str | None = None
    claude_code_version: str | None = None
    unavailable_reason: str = ""

    @property
    def raw_sdk_version(self) -> str | None:
        return self.sdk_version


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _sdk_version() -> str | None:
    try:
        return version("claude-agent-sdk")
    except PackageNotFoundError:
        return None


def claude_code_version(*, cli_path: str | Path | None = None, timeout_sec: float = 2.0) -> str | None:
    if cli_path is None:
        try:
            import claude_agent_sdk

            cli_path = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
        except Exception:
            return None
    try:
        result = subprocess.run(
            [str(cli_path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=max(0.2, timeout_sec),
        )
    except Exception:
        return None
    output: str = (result.stdout or result.stderr or "").strip()
    return output or None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw: str = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed: datetime = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _object(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return value
    return None


def _rate_limit_window(
    rate_limits: dict[str, object] | None,
    name: str,
) -> tuple[float | None, datetime | None]:
    if rate_limits is None:
        return None, None
    window: dict[str, object] | None = _object(rate_limits.get(name))
    if window is None:
        return None, None
    return _number(window.get("utilization")), _parse_datetime(window.get("resets_at"))


def _scoped_limit_from_raw(item: object) -> ClaudeScopedLimit | None:
    data: dict[str, object] | None = _object(item)
    if data is None:
        return None
    scope: dict[str, object] | None = _object(data.get("scope"))
    model: dict[str, object] | None = _object(scope.get("model")) if scope else None
    return ClaudeScopedLimit(
        kind=data.get("kind") if isinstance(data.get("kind"), str) else None,
        group=data.get("group") if isinstance(data.get("group"), str) else None,
        percent=_number(data.get("percent")),
        resets_at=_parse_datetime(data.get("resets_at")),
        model_id=model.get("id") if model and isinstance(model.get("id"), str) else None,
        model_display_name=(
            model.get("display_name")
            if model and isinstance(model.get("display_name"), str)
            else None
        ),
        is_active=data.get("is_active") if isinstance(data.get("is_active"), bool) else None,
        raw=data,
    )


async def read_claude_usage_raw(client: object, *, timeout: float = 60.0) -> dict[str, object]:
    """Read raw Claude Code ``get_usage`` data from an active SDK client.

    The Python SDK 0.2.110 does not expose a public wrapper for this control
    request. The narrow private dependency is ``client._query._send_control_request``,
    which is the same internal path used by public methods like
    ``ClaudeSDKClient.get_context_usage()``.
    """
    query: object | None = getattr(client, "_query", None)
    if query is None:
        raise RuntimeError("claude_sdk_query_unavailable")
    send = getattr(query, "_send_control_request", None)
    if send is None:
        raise RuntimeError("claude_sdk_control_request_unavailable")
    response: object = await send({"subtype": "get_usage"}, timeout=timeout)
    if not isinstance(response, dict):
        raise RuntimeError("claude_get_usage_response_not_object")
    return response


def normalize_claude_quota(
    raw: dict[str, object],
    *,
    observed_at: datetime | None = None,
    sdk_version: str | None = None,
    claude_version: str | None = None,
) -> ClaudeQuotaSnapshot:
    rate_limits: dict[str, object] | None = _object(raw.get("rate_limits"))
    five_util, five_reset = _rate_limit_window(rate_limits, "five_hour")
    seven_util, seven_reset = _rate_limit_window(rate_limits, "seven_day")
    scoped_limits: list[ClaudeScopedLimit] = []
    limits: object = rate_limits.get("limits") if rate_limits else None
    if isinstance(limits, list):
        for item in limits:
            scoped: ClaudeScopedLimit | None = _scoped_limit_from_raw(item)
            if scoped is not None:
                scoped_limits.append(scoped)

    available: object = raw.get("rate_limits_available")
    has_any_window: bool = any(
        value is not None for value in (five_util, five_reset, seven_util, seven_reset)
    )
    status: Literal["valid", "unavailable", "stale"] = (
        "valid" if available is True and has_any_window else "unavailable"
    )
    reason: str = "" if status == "valid" else "claude_rate_limits_unavailable_or_empty"
    subscription: object = raw.get("subscription_type")
    return ClaudeQuotaSnapshot(
        status=status,
        observed_at=observed_at or _utc_now(),
        five_hour_utilization=five_util,
        five_hour_resets_at=five_reset,
        seven_day_utilization=seven_util,
        seven_day_resets_at=seven_reset,
        scoped_limits=scoped_limits,
        subscription_type=subscription if isinstance(subscription, str) else None,
        rate_limits_available=available if isinstance(available, bool) else None,
        sdk_version=sdk_version or _sdk_version(),
        claude_code_version=claude_version,
        unavailable_reason=reason,
    )


async def read_claude_quota_snapshot(
    client: object,
    *,
    timeout: float = 60.0,
) -> ClaudeQuotaSnapshot:
    try:
        raw: dict[str, object] = await read_claude_usage_raw(client, timeout=timeout)
    except Exception as exc:
        return ClaudeQuotaSnapshot(
            status="unavailable",
            observed_at=_utc_now(),
            sdk_version=_sdk_version(),
            unavailable_reason=f"claude_get_usage_failed:{type(exc).__name__}",
        )
    return normalize_claude_quota(raw, observed_at=_utc_now())


async def open_claude_window_with_minimal_turn(
    *,
    model: str = "haiku",
    cli_path: str | Path | None = None,
    timeout: float = 120.0,
    max_budget_usd: float = 0.05,
) -> dict[str, object]:
    """Send ONE isolated, minimal model turn — the cheapest thing that can start
    a subscription window.

    This is the only place in the repo that spends tokens without a user asking
    for work, so every knob here is a containment decision, per
    ``docs/SESSION_WINDOW_WARMING_SPEC.md`` §12:

      * ``setting_sources=[]`` + ``mcp_servers={}`` — no user/project settings, no
        CLAUDE.md, no MCP schemas. Those are what make a "tiny prompt" expensive.
      * ``allowed_tools=[]`` / ``tools=[]`` — one turn, no tool loop.
      * an EMPTY temp directory as cwd — never a user repository.
      * ``max_turns=1`` + ``max_budget_usd`` — bounded twice.
      * no session persistence: a one-shot ``query()``, never a resumable session.

    Returns ``{ok, usd, session_id, subtype, error}``. Never raises: the caller is
    a background scheduler and a failed activation is a skipped window, not an
    incident.
    """
    import tempfile

    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    out: dict[str, object] = {"ok": False, "usd": None, "session_id": None,
                              "subtype": None, "error": ""}
    try:
        with tempfile.TemporaryDirectory(prefix="ai-team-quota-prewarm-") as tmp:
            options = ClaudeAgentOptions(
                model=model,
                cwd=tmp,
                tools=[],
                allowed_tools=[],
                mcp_servers={},
                setting_sources=[],
                permission_mode="dontAsk",
                max_turns=1,
                max_budget_usd=max_budget_usd,
                effort="low",
                env={
                    **os.environ,
                    "CLAUDE_AGENT_SDK_CLIENT_APP": "ai-team-quota-prewarmer/0",
                },
                **({"cli_path": str(cli_path)} if cli_path else {}),
            )

            async def _run() -> None:
                async for message in query(prompt=_ACTIVATION_PROMPT, options=options):
                    if isinstance(message, ResultMessage):
                        out["ok"] = not bool(getattr(message, "is_error", False))
                        out["usd"] = getattr(message, "total_cost_usd", None)
                        out["session_id"] = getattr(message, "session_id", None)
                        out["subtype"] = getattr(message, "subtype", None)
                        return

            await asyncio.wait_for(_run(), timeout=max(5.0, timeout))
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}"
    return out


#: Deterministic and boring on purpose (spec §6A): a provider inspecting this
#: traffic should see a user-authorized scheduler, not something imitating a
#: person.
_ACTIVATION_PROMPT: str = "Return only: 0. Do not use tools."


async def read_claude_usage_raw_with_new_client(
    *,
    cwd: str | Path | None = None,
    cli_path: str | Path | None = None,
    timeout: float = 60.0,
) -> dict[str, object]:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    options = ClaudeAgentOptions(
        cwd=Path(cwd).resolve() if cwd is not None else None,
        allowed_tools=[],
        permission_mode="dontAsk",
        env={
            **os.environ,
            "CLAUDE_AGENT_SDK_CLIENT_APP": "ai-team-quota-coordinator/0",
        },
        **({"cli_path": str(cli_path)} if cli_path else {}),
    )
    client = ClaudeSDKClient(options=options)
    await client.connect()
    try:
        return await read_claude_usage_raw(client, timeout=timeout)
    finally:
        await client.disconnect()
