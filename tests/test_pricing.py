"""Unit tests for the approximate LLM cost estimator (src.services.pricing).

Pure math + honesty-first behavior — no backend, no DB.
"""
from src.services.pricing import TokenTotals, estimate_cost, priced_models


def _one_million_each() -> TokenTotals:
    t = TokenTotals(
        input=1_000_000,
        output=1_000_000,
        cache_read=1_000_000,
        cache_creation=1_000_000,
    )
    t.total = 4_000_000
    return t


def test_opus_rates_and_cache_math():
    est = estimate_cost("claude-opus-4-1", _one_million_each())
    assert est.known is True
    # base $5/$25 per MTok; cache write 1.25x input, cache read 0.10x input.
    assert est.usd_input == 5.0
    assert est.usd_output == 25.0
    assert est.usd_cache_write == 6.25
    assert est.usd_cache_read == 0.5
    assert est.usd_total == 36.75


def test_sonnet_family_substring_match():
    est = estimate_cost("claude-sonnet-4-6", _one_million_each())
    assert est.known is True
    # 3 + 15 + 3.75 + 0.30
    assert est.usd_total == 22.05


def test_haiku_family():
    est = estimate_cost("haiku", _one_million_each())
    assert est.known is True
    # 1 + 5 + 1.25 + 0.10
    assert est.usd_total == 7.35


def test_unknown_model_is_honest_not_fabricated():
    est = estimate_cost("gpt-5-codex", _one_million_each())
    assert est.known is False
    assert est.usd_total is None
    assert est.reason == "unknown_model_pricing"


def test_no_model_reason():
    est = estimate_cost(None, _one_million_each())
    assert est.known is False
    assert est.reason == "no_model"


def test_zero_totals_known_model_is_zero_cost():
    est = estimate_cost("opus", TokenTotals())
    assert est.known is True
    assert est.usd_total == 0.0


def test_priced_families_present():
    assert set(priced_models()) == {"opus", "sonnet", "haiku"}
