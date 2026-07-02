"""Backend Account + Usage Visibility (#30/#33).

Aggregates ONLY what a backend can prove locally right now:
- the configured backend set (registry) + each backend's configured/default model
- recent observed model(s) and token usage summed from the LLM telemetry turns
- explicit coverage/reason fields for everything we CANNOT prove

Hard rule (from the dispatch + architecture rules): **never invent quota data.**
Daily/weekly limits, reset times, and account identity are NOT emitted by any
current backend, so they are returned as ``null`` with a machine-readable reason.
Observed token *usage* is not a *limit* — we never derive a limit from usage.

This is a read-only projection. It does no network I/O and bounds its DB reads.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Token fields we know how to sum if a backend's usage dict carries them. Unknown
# keys are ignored; missing keys contribute nothing (never fabricated).
_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "total_tokens",
)

# Why a limit/identity field is null. These are honest, not placeholders.
_REASON_NO_LIMIT_SOURCE = "no_backend_limit_source"
_REASON_NO_IDENTITY_SOURCE = "no_backend_identity_source"


def _configured_model(cfg: Any, backend: str) -> Optional[str]:
    """Best-effort read of a backend's configured/default model from config."""
    key_map = {
        "claude": ("claude", "default_model"),
        "codex": ("codex", "default_model"),
        "opencode": ("opencode", "default_model"),
        "opencode-server": ("opencode", "default_model"),
    }
    section, attr = key_map.get(backend, (None, None))
    if section is None:
        return None
    try:
        return getattr(getattr(cfg, section, None), attr, None)
    except Exception:
        return None


def _sum_usage(dst: Dict[str, int], usage: Any) -> None:
    if not isinstance(usage, dict):
        return
    for key in _TOKEN_KEYS:
        val = usage.get(key)
        if isinstance(val, (int, float)):
            dst[key] = dst.get(key, 0) + int(val)


def build_backend_usage(
    cfg: Any,
    *,
    valid_backends: List[str],
    telemetry_store: Any = None,
    turn_limit: int = 200,
) -> Dict[str, Any]:
    """Produce the honest per-backend usage/status view.

    ``telemetry_store`` may be None (telemetry disabled / unavailable) — the view
    then reports registry/config facts only and marks usage coverage as
    telemetry-unavailable rather than empty.
    """
    telemetry_available = telemetry_store is not None
    backends: List[Dict[str, Any]] = []

    for name in valid_backends:
        entry: Dict[str, Any] = {
            "backend": name,
            "configured_model": _configured_model(cfg, name),
            "observed_models": [],
            "recent_usage": None,          # summed tokens from recent turns, or null
            "recent_turn_count": 0,
            # Facts no backend proves locally — honest nulls, not zeros:
            "account_identity": None,
            "account_identity_reason": _REASON_NO_IDENTITY_SOURCE,
            "daily_limit": None,
            "weekly_limit": None,
            "limit_reset_at": None,
            "limit_reason": _REASON_NO_LIMIT_SOURCE,
            "usage_coverage": "telemetry_unavailable" if not telemetry_available else "no_data",
        }

        if telemetry_available:
            try:
                turns = telemetry_store.list_turns(backend=name, limit=turn_limit)
            except Exception as e:
                logger.debug("event=backend_usage_list_turns_failed backend=%s err=%s", name, e)
                turns = []

            if turns:
                usage_totals: Dict[str, int] = {}
                observed: List[str] = []
                for turn in turns:
                    for m in turn.get("observed_models") or []:
                        if m and m not in observed:
                            observed.append(m)
                    rm = turn.get("requested_model")
                    if rm and rm not in observed:
                        observed.append(rm)
                    # metrics carries the turn's rolled-up usage when present.
                    _sum_usage(usage_totals, turn.get("metrics"))
                entry["observed_models"] = observed[:10]
                entry["recent_turn_count"] = len(turns)
                if usage_totals:
                    entry["recent_usage"] = usage_totals
                    entry["usage_coverage"] = "observed"
                else:
                    # We have turns but no usable token fields — say so honestly.
                    entry["usage_coverage"] = "usage_fields_absent"

        backends.append(entry)

    return {
        "telemetry_available": telemetry_available,
        "backends": backends,
        # Global honesty banner for the UI: no provider quota source exists yet.
        "limits_source": None,
        "limits_reason": _REASON_NO_LIMIT_SOURCE,
    }
