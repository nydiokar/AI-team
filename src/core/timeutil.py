"""One clock for the whole gateway — UTC-aware storage, local render at the edge.

Rationale (DROP_TIMEZONE_NATIVE_TIME, 2026-07-13): timestamps used to be written
in MIXED representations — `session_store.py` wrote naive-LOCAL
(`datetime.now().isoformat()`), while `db.py` wrote UTC-aware
(`datetime.now(timezone.utc)`). A single entity then carried two clocks, so any
render that mixed the fields (or any string comparison) was wrong, and "timezone"
kept getting (wrongly) blamed for bugs.

The fix is one convention, enforced through one helper:
  * **Store** UTC-aware ISO everywhere (`now_iso()`), so every stored timestamp is
    unambiguous and directly comparable.
  * **Render** in the operator's local zone at the boundary only (the Web UI's
    `new Date(iso)` already does this correctly for a tz-aware string).

`parse_iso()` tolerates legacy rows: a naive timestamp (no offset) is interpreted
as LOCAL — which is exactly what the old `session_store` writer meant — and
returned tz-aware, so subtraction against a UTC-aware `now` never raises.
"""
from __future__ import annotations

from datetime import datetime, timezone


def now_iso() -> str:
    """The single source of 'now' for any stored timestamp: UTC-aware ISO-8601
    (e.g. ``2026-07-13T10:15:42.123456+00:00``)."""
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    """Parse an ISO timestamp to a tz-AWARE datetime.

    A value with an offset is honored as-is. A naive value (legacy
    `session_store` rows) is interpreted as LOCAL time and made aware, so it can
    be compared/subtracted against a UTC-aware ``now`` without a
    naive-vs-aware ``TypeError``.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        # Naive == legacy local wall-clock; attach the local zone.
        return dt.astimezone()
    return dt
