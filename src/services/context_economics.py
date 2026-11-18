"""Pure, bounded context advice and intervention accounting over durable telemetry.

USD values are API equivalents using the repository price table, not subscription
charges. A context reduction is observed; future avoided spend is a scenario.
"""
from __future__ import annotations

from pydantic import BaseModel

from src.services.pricing import TokenTotals, estimate_cost

COMPACTION_THRESHOLD: float = 0.80


def context_metrics(turn: dict[str, object]) -> dict[str, object]:
    """Reject persisted pre-v4 Claude context without rewriting billing history."""
    raw = turn.get("metrics")
    metrics = raw if isinstance(raw, dict) else {}
    version = turn.get("projection_version")
    if turn.get("backend") in (None, "claude") and isinstance(version, int) and version < 4:
        return {**metrics, **{key: None for key in (
            "turn_entry_context_tokens", "turn_exit_context_tokens", "peak_context_tokens", "context_tokens",
            "context_window_tokens", "context_used_ratio", "context_remaining_tokens",
        )}}
    return metrics


class ContextEconomics(BaseModel):
    context_tokens: int | None = None
    context_window_tokens: int | None = None
    context_used_ratio: float | None = None
    recommendation: str = "unavailable"
    threshold: float = COMPACTION_THRESHOLD
    cold_write_usd: float | None = None
    warm_read_usd: float | None = None
    cold_premium_usd: float | None = None
    cost_basis: str = "api_equivalent_5m_cache_write_excludes_output_and_new_input"


def context_economics(tokens: int | None, window: int | None, model: str | None) -> ContextEconomics:
    result = ContextEconomics(context_tokens=tokens, context_window_tokens=window)
    if tokens is None or tokens < 0:
        return result
    if window is not None and window > 0:
        result.context_used_ratio = round(tokens / window, 6)
        result.recommendation = "compact" if tokens / window >= COMPACTION_THRESHOLD else "continue"
    if not model or not any(family in model.lower() for family in ("opus", "sonnet", "haiku", "fable", "mythos")):
        result.cost_basis = "cold_cache_policy_unavailable_for_model"
        return result
    cost = estimate_cost(model, TokenTotals(cache_creation=tokens, cache_read=tokens))
    if cost.known:
        result.cold_write_usd = cost.usd_cache_write
        result.warm_read_usd = cost.usd_cache_read
        result.cold_premium_usd = round((cost.usd_cache_write or 0) - (cost.usd_cache_read or 0), 6)
    return result


class CompactionOutcome(BaseModel):
    task_id: str
    status: str
    before_tokens: int | None = None
    after_tokens: int | None = None
    reclaimed_tokens: int | None = None
    reclaimed_ratio: float | None = None
    avoided_read_usd_per_request: float | None = None
    avoided_write_usd_per_request: float | None = None
    observed_compaction_usd: float | None = None
    break_even_warm_requests: int | None = None
    retained_rewrite_premium_usd: float | None = None
    cost_basis: str = "api_equivalent_not_realized_savings"


def compaction_outcome(
    task_id: str, status: str, before: int | None, after: int | None,
    model: str | None, compaction_usd: float | None,
) -> CompactionOutcome:
    result = CompactionOutcome(task_id=task_id, status=status, before_tokens=before,
                               after_tokens=after, observed_compaction_usd=compaction_usd)
    if status != "completed" or before is None or after is None:
        return result
    result.reclaimed_tokens = before - after
    result.reclaimed_ratio = round((before - after) / before, 6) if before else None
    # Negative deltas remain visible: an intervention need not save anything.
    cost = estimate_cost(model, TokenTotals(cache_read=before - after, cache_creation=before - after))
    if cost.known:
        result.avoided_read_usd_per_request = cost.usd_cache_read
        if context_economics(after, None, model).cold_write_usd is not None:
            result.avoided_write_usd_per_request = cost.usd_cache_write
        if compaction_usd is not None and (cost.usd_cache_read or 0) > 0:
            from math import ceil
            retained = context_economics(after, None, model)
            if retained.cold_premium_usd is not None:
                result.retained_rewrite_premium_usd = retained.cold_premium_usd
                result.break_even_warm_requests = ceil((compaction_usd + retained.cold_premium_usd) / cost.usd_cache_read)
    return result


def observed_turn_cost(turn: dict[str, object] | None) -> float | None:
    """Price recorded buckets, never a context-size counter or an assumed zero."""
    if turn is None:
        return None
    metrics = turn.get("metrics")
    if isinstance(metrics, dict):
        from math import isfinite
        reported = metrics.get("provider_reported_cost_usd")
        if type(reported) in (int, float) and 0 <= reported <= 2**63 - 1 and isfinite(reported):
            return float(reported)
    version = turn.get("projection_version")
    if turn.get("backend") in (None, "claude") and isinstance(version, int) and version < 4:
        return None  # legacy uncached-input projection cannot price an observed turn
    if not isinstance(metrics, dict) or any(metrics.get(key) is None for key in (
        "input_tokens", "uncached_input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
    )) or turn.get("final_status") not in ("success", "failed", "completed", "cancelled"):
        return None
    usage_coverage = turn.get("coverage")
    if isinstance(usage_coverage, dict) and (usage_coverage.get("usage") or {}).get("coverage") == "partial":
        return None
    model = turn_model(turn)
    if metrics.get("cache_creation_tokens") and context_economics(0, None, model).cold_write_usd is None:
        return None  # no provider-specific write policy; never borrow Claude's premium
    cost = estimate_cost(model, TokenTotals(
        input=metrics.get("uncached_input_tokens") or 0,
        output=metrics.get("output_tokens") or 0,
        cache_read=metrics.get("cache_read_tokens") or 0,
        cache_creation=metrics.get("cache_creation_tokens") or 0,
    ))
    return cost.usd_total if cost.known else None


def turn_model(turn: dict[str, object]) -> str | None:
    models = turn.get("observed_models")
    if isinstance(models, list):
        return str(models[0]) if len(models) == 1 else None
    return str(turn.get("model") or turn.get("requested_model") or "") or None
