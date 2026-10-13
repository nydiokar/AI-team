"""Shared OpenAPI response models for the Control API routers.

Why this module exists
----------------------
Handlers across ``src/control/routes`` return raw ``JSONResponse`` objects. When
a FastAPI route declares no response schema, the generated OpenAPI emits an empty
``{}`` schema for the success body, which browsers/explorers render as ``null`` —
misleading, because the server always sends a real JSON body.

The fix (repo convention, first used in ``sessions.py``) is to attach the schema
via ``responses={200: {"model": XOut, "description": ...}}`` on the decorator.
Because the handler still returns ``JSONResponse`` directly, FastAPI passes the
body through untouched — the model documents the shape **without** re-serialising
or filtering any field. These primitives are the cross-router building blocks;
per-router shapes live next to their routes.

Coverage of the whole ``/api/*`` surface is enforced by
``tests/test_response_model_coverage.py`` — a new endpoint with no declared
success schema fails that test, so the docs can no longer silently drift.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class CommandResult(BaseModel):
    """The lifecycle/mutation success envelope. ``ok:false`` with a ``reason`` is
    a structured refusal carried on a 200, not an HTTP error."""

    ok: bool
    reason: Optional[str] = None


class TokenTotals(BaseModel):
    """Token breakdown shared by the cost/usage/roster projections."""

    input: int
    output: int
    cache_read: int
    cache_creation: int
    total: int


class UsdSummary(BaseModel):
    """Aggregated USD spend with honest pricing coverage (unpriced tokens are
    surfaced, never silently dropped)."""

    known: float
    unpriced_tokens: int
    coverage_pct: float
