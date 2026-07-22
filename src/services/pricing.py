"""Approximate LLM token cost estimation (Cockpit — session cost visibility).

Honesty-first, mirroring ``backend_usage.py``: when a session's model cannot be
mapped to a known price table we return ``known=False`` and a ``null`` cost with
a reason, never a fabricated number.

Prices are per **million tokens** in USD. Cache economics follow Anthropic's
published structure (prompt-caching): a cache *write* (creation) bills at 1.25×
the base input rate (5-minute ephemeral, the Claude Code default) and a cache
*read* at ~0.10× the base input rate. Base input/output rates per model family:
Opus $5/$25, Sonnet $3/$15, Haiku $1/$5 per MTok.

Update the table below when Anthropic revises pricing — it is the single source
of truth for cost math in the gateway.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel

_CACHE_WRITE_MULTIPLIER: float = 1.25
_CACHE_READ_MULTIPLIER: float = 0.10


class PriceRates(BaseModel):
    """Per-million-token USD rates for one model family."""
    input: float
    output: float
    cache_write: float
    cache_read: float


def _family(input_usd: float, output_usd: float) -> PriceRates:
    return PriceRates(
        input=input_usd,
        output=output_usd,
        cache_write=round(input_usd * _CACHE_WRITE_MULTIPLIER, 4),
        cache_read=round(input_usd * _CACHE_READ_MULTIPLIER, 4),
    )


# Substring → rates. Keys are matched against a lowercased model id; order does
# not matter (families are disjoint). Covers the Claude 4.x families used by the
# Claude Code backend today.
_PRICE_TABLE: Dict[str, PriceRates] = {
    "opus": _family(5.0, 25.0),
    "sonnet": _family(3.0, 15.0),
    "haiku": _family(1.0, 5.0),
}


class TokenTotals(BaseModel):
    """Per-session token totals (mirrors db.get_session_token_totals + grand sum)."""
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    total: int = 0  # grand total across all four buckets


class CostEstimate(BaseModel):
    """Approximate USD cost for a set of token totals under one model's rates."""
    known: bool
    model_priced: Optional[str] = None      # the model id the rates were keyed on
    reason: Optional[str] = None            # populated when known is False
    currency: str = "USD"
    usd_total: Optional[float] = None
    usd_input: Optional[float] = None
    usd_output: Optional[float] = None
    usd_cache_write: Optional[float] = None
    usd_cache_read: Optional[float] = None
    rates: Optional[PriceRates] = None


def _match_rates(model: Optional[str]) -> Optional[PriceRates]:
    if not model:
        return None
    needle: str = model.lower()
    for key, rates in _PRICE_TABLE.items():
        if key in needle:
            return rates
    return None


def priced_models() -> List[str]:
    """The model families this module can price (for diagnostics/tests)."""
    return list(_PRICE_TABLE.keys())


def estimate_cost(model: Optional[str], totals: TokenTotals) -> CostEstimate:
    """Estimate USD cost for ``totals`` under ``model``'s rates.

    Returns an honest ``known=False`` estimate (null cost + reason) when the
    model cannot be mapped — never a fabricated number.
    """
    rates: Optional[PriceRates] = _match_rates(model)
    if rates is None:
        return CostEstimate(
            known=False,
            model_priced=model,
            reason="unknown_model_pricing" if model else "no_model",
        )

    per_million: float = 1_000_000.0
    usd_input: float = round(totals.input / per_million * rates.input, 6)
    usd_output: float = round(totals.output / per_million * rates.output, 6)
    usd_cache_write: float = round(
        totals.cache_creation / per_million * rates.cache_write, 6
    )
    usd_cache_read: float = round(totals.cache_read / per_million * rates.cache_read, 6)
    usd_total: float = round(
        usd_input + usd_output + usd_cache_write + usd_cache_read, 6
    )
    return CostEstimate(
        known=True,
        model_priced=model,
        usd_total=usd_total,
        usd_input=usd_input,
        usd_output=usd_output,
        usd_cache_write=usd_cache_write,
        usd_cache_read=usd_cache_read,
        rates=rates,
    )
