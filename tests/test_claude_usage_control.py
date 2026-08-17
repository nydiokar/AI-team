from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.services.claude_usage_control import (
    normalize_claude_quota,
    read_claude_quota_snapshot,
    read_claude_usage_raw,
)


class _FakeQuery:
    def __init__(self, response: dict[str, object]) -> None:
        self.response: dict[str, object] = response
        self.requests: list[dict[str, object]] = []
        self.timeouts: list[float] = []

    async def _send_control_request(
        self,
        request: dict[str, object],
        timeout: float = 60.0,
    ) -> dict[str, object]:
        self.requests.append(request)
        self.timeouts.append(timeout)
        return self.response


class _FakeClient:
    def __init__(self, response: dict[str, object]) -> None:
        self._query = _FakeQuery(response)


@pytest.mark.asyncio
async def test_read_claude_usage_raw_sends_get_usage_control_request() -> None:
    client = _FakeClient({"rate_limits_available": False})

    raw = await read_claude_usage_raw(client, timeout=3.5)

    assert raw == {"rate_limits_available": False}
    assert client._query.requests == [{"subtype": "get_usage"}]
    assert client._query.timeouts == [3.5]


def test_normalize_claude_quota_preserves_limits_array() -> None:
    raw: dict[str, object] = {
        "subscription_type": "max",
        "rate_limits_available": True,
        "rate_limits": {
            "five_hour": {
                "utilization": 12,
                "resets_at": "2026-08-17T02:20:00.013395+00:00",
            },
            "seven_day": {
                "utilization": 40,
                "resets_at": "2026-08-20T22:00:00.013416+00:00",
            },
            "limits": [
                {
                    "kind": "weekly_scoped",
                    "group": "weekly",
                    "percent": 5,
                    "resets_at": "2026-08-20T22:00:00.013623+00:00",
                    "scope": {"model": {"id": None, "display_name": "Fable"}},
                    "is_active": False,
                }
            ],
        },
    }

    snapshot = normalize_claude_quota(
        raw,
        observed_at=datetime(2026, 8, 16, 22, 0, tzinfo=timezone.utc),
        sdk_version="0.2.110",
    )

    assert snapshot.status == "valid"
    assert snapshot.five_hour_utilization == 12
    assert snapshot.five_hour_resets_at == datetime(
        2026, 8, 17, 2, 20, 0, 13395, tzinfo=timezone.utc
    )
    assert snapshot.seven_day_utilization == 40
    assert snapshot.scoped_limits[0].kind == "weekly_scoped"
    assert snapshot.scoped_limits[0].model_display_name == "Fable"
    assert snapshot.raw_sdk_version == "0.2.110"


@pytest.mark.asyncio
async def test_read_claude_quota_snapshot_failure_is_unavailable() -> None:
    snapshot = await read_claude_quota_snapshot(object())

    assert snapshot.status == "unavailable"
    assert snapshot.five_hour_utilization is None
    assert snapshot.unavailable_reason.startswith("claude_get_usage_failed:")
