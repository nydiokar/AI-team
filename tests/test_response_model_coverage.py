"""Forcing function: every ``/api/*`` endpoint must declare a success-response
schema.

Background
----------
FastAPI emits an empty ``{}`` success schema for any route that declares no
response model. Browsers/OpenAPI explorers render that empty schema as ``null``,
which misrepresents endpoints that always return a real JSON body (see the push,
approvals, cost-explorer and turn-request endpoints). This test walks the live
OpenAPI document the app actually serves and fails if any ``/api/*`` operation's
success response lacks a meaningful schema.

How to satisfy it when you add an endpoint
------------------------------------------
Attach the shape to the decorator without changing the handler's return:

    @router.post("/api/thing", responses={200: {"model": ThingOut}})
    def api_thing(...) -> JSONResponse:
        return JSONResponse(ThingOut(...).model_dump())

Define ``ThingOut`` as a Pydantic model (reuse the shared primitives in
``src/control/routes/_responses.py`` where they fit). Do **not** paper over this
with ``-> Dict[str, Any]`` / an empty model — the test rejects schemas with no
properties precisely so the contract cannot be gamed.
"""
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from src.control.control_api import build_control_api

# Genuinely non-JSON success bodies. Each entry needs a reason — this list is a
# ratchet: it may shrink, and anything added here is reviewed, never reflexive.
STREAMING_EXCEPTIONS: dict[str, str] = {
    "/api/events/stream": "Server-Sent Events (text/event-stream), not a JSON body.",
}

SUCCESS_CODES = ("200", "201", "202")


def _success_schema(operation: Dict[str, Any]) -> Dict[str, Any] | None:
    """Return the application/json success schema for an operation, or None."""
    responses = operation.get("responses", {})
    for code in SUCCESS_CODES:
        if code in responses:
            content = responses[code].get("content", {})
            js = content.get("application/json")
            if js is None:
                return None
            return js.get("schema", {})
    return None


def _is_meaningful(schema: Dict[str, Any] | None) -> bool:
    """A schema is meaningful if it names a component ($ref), composes them
    (allOf/anyOf/oneOf), or is an object/array with declared structure. A bare
    ``{}`` or a propertyless ``{"type": "object"}`` (what ``Dict[str, Any]``
    yields) is NOT meaningful."""
    if not schema:
        return False
    if "$ref" in schema or any(k in schema for k in ("allOf", "anyOf", "oneOf")):
        return True
    if schema.get("type") == "array":
        return bool(schema.get("items"))
    if schema.get("type") == "object":
        return bool(schema.get("properties"))
    # A primitive-typed body (e.g. {"type": "string"}) is an explicit shape.
    return "type" in schema


def _api_operations() -> list[tuple[str, str, Dict[str, Any]]]:
    app = build_control_api(MagicMock())
    spec = app.openapi()
    ops: list[tuple[str, str, Dict[str, Any]]] = []
    for path, item in spec.get("paths", {}).items():
        if not path.startswith("/api/"):
            continue
        for method, operation in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            ops.append((path, method.upper(), operation))
    return ops


def test_every_api_endpoint_declares_a_success_schema() -> None:
    offenders: list[str] = []
    for path, method, operation in _api_operations():
        if path in STREAMING_EXCEPTIONS:
            continue
        if not _is_meaningful(_success_schema(operation)):
            offenders.append(f"{method} {path}")
    assert not offenders, (
        "These /api/* endpoints have no meaningful success-response schema, so "
        "their OpenAPI docs render the 200 body as `null`. Attach "
        "`responses={200: {'model': XOut}}` (see this file's docstring):\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_streaming_exceptions_are_still_routes() -> None:
    """Keep the allowlist honest: a path listed as a streaming exception must
    still exist, otherwise it is stale and should be removed."""
    paths = {path for path, _, _ in _api_operations()}
    stale = [p for p in STREAMING_EXCEPTIONS if p not in paths]
    assert not stale, f"Stale streaming exceptions (no longer routes): {stale}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
