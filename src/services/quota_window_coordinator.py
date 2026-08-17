"""Observe-only quota window coordinator.

Phase 1 deliberately does not activate provider sessions. The coordinator calls
provider-owned adapters for telemetry, persists sanitized snapshots, emits
structured events, and exposes a read-only status model.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Generator, Iterable, List, Optional, Protocol

logger = logging.getLogger(__name__)
_SNAPSHOT_STALE_AFTER = timedelta(minutes=15)


class WindowSemantics(Enum):
    ANCHORED = "anchored"
    FIXED = "fixed"
    SLIDING = "sliding"
    TOKEN_BUCKET = "token_bucket"
    UNKNOWN = "unknown"


class TelemetryQuality(Enum):
    AUTHORITATIVE = "authoritative"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class QuotaPrincipal:
    provider: str
    principal_hash: str
    label: str = ""
    authentication_mode: str = "unknown"


@dataclass(frozen=True)
class QuotaBucket:
    provider: str
    bucket_id: str
    bucket_name: str = ""
    principal_hash: str = ""
    window_semantics: WindowSemantics = WindowSemantics.UNKNOWN
    telemetry_quality: TelemetryQuality = TelemetryQuality.UNAVAILABLE
    window_duration_seconds: Optional[int] = None


@dataclass(frozen=True)
class QuotaSnapshot:
    provider: str
    bucket_id: str
    principal_hash: str
    observed_at: datetime
    telemetry_quality: TelemetryQuality
    used_percent: Optional[float] = None
    reset_at: Optional[datetime] = None
    limit_reached: Optional[bool] = None
    window_duration_seconds: Optional[int] = None
    raw_status: str = ""
    unavailable_reason: str = ""


@dataclass(frozen=True)
class AdapterCapability:
    provider: str
    adapter_version: str
    schema_version: str
    can_observe: bool
    supports_active_session_detection: bool = False
    telemetry_quality: TelemetryQuality = TelemetryQuality.UNAVAILABLE
    notes: str = ""


@dataclass(frozen=True)
class QuotaAdapterStatus:
    provider: str
    enabled: bool
    status: str
    reason: str = ""
    adapter_version: str = ""
    schema_version: str = ""
    last_checked_at: Optional[datetime] = None


class QuotaAdapter(Protocol):
    async def identify_principal(self) -> QuotaPrincipal: ...
    async def discover_buckets(self) -> list[QuotaBucket]: ...
    async def observe(self, bucket_id: str) -> QuotaSnapshot: ...
    async def detect_active_user_session(self) -> bool | None: ...
    async def capabilities(self) -> AdapterCapability: ...


class QuotaAdapterError(Exception):
    def __init__(self, reason: str, *, quality: TelemetryQuality = TelemetryQuality.UNAVAILABLE) -> None:
        super().__init__(reason)
        self.reason = reason
        self.quality = quality


QuotaEventHandler = Callable[[str, Dict[str, Any]], None]


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def normalize_utc(value: datetime | str | None) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    else:
        dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utc_iso(value: datetime | str | None) -> str:
    dt = normalize_utc(value)
    if dt is None:
        return ""
    return dt.isoformat().replace("+00:00", "Z")


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _snapshot_identity(snapshot: QuotaSnapshot) -> str:
    parts = [
        snapshot.provider,
        snapshot.principal_hash,
        snapshot.bucket_id,
        utc_iso(snapshot.observed_at),
        utc_iso(snapshot.reset_at),
        "" if snapshot.used_percent is None else f"{snapshot.used_percent:.8f}",
        str(snapshot.limit_reached),
        snapshot.telemetry_quality.value,
        snapshot.raw_status,
        snapshot.unavailable_reason,
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _principal_hash(provider: str, key: str) -> str:
    source = f"{provider}\x1f{key or 'principal_unknown'}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _coerce_percent(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return None
    if pct < 0:
        return 0.0
    if pct > 100:
        return 100.0
    return pct


def _is_stale_reset(reset_at: datetime | None, observed_at: datetime, *, grace: timedelta = timedelta(minutes=5)) -> bool:
    if reset_at is None:
        return False
    reset = normalize_utc(reset_at)
    observed = normalize_utc(observed_at)
    if reset is None or observed is None:
        return False
    return reset < observed - grace


def _coerce_reset_at(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            if raw.isdigit():
                return datetime.fromtimestamp(float(raw), tz=timezone.utc)
            return normalize_utc(raw)
        except Exception:
            return None
    return None


_CURRENT_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS principals (
    provider TEXT NOT NULL,
    principal_hash TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    authentication_mode TEXT NOT NULL DEFAULT 'unknown',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (provider, principal_hash)
);

CREATE TABLE IF NOT EXISTS buckets (
    provider TEXT NOT NULL,
    principal_hash TEXT NOT NULL,
    bucket_id TEXT NOT NULL,
    bucket_name TEXT NOT NULL DEFAULT '',
    window_semantics TEXT NOT NULL DEFAULT 'unknown',
    telemetry_quality TEXT NOT NULL DEFAULT 'unavailable',
    window_duration_seconds INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (provider, principal_hash, bucket_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    principal_hash TEXT NOT NULL,
    bucket_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    telemetry_quality TEXT NOT NULL,
    used_percent REAL,
    reset_at TEXT,
    limit_reached INTEGER,
    window_duration_seconds INTEGER,
    raw_status TEXT NOT NULL DEFAULT '',
    unavailable_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_bucket_observed
    ON snapshots(provider, principal_hash, bucket_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS adapter_status (
    provider TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    adapter_version TEXT NOT NULL DEFAULT '',
    schema_version TEXT NOT NULL DEFAULT '',
    last_checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coordinator_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_name TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    principal_hash TEXT NOT NULL DEFAULT '',
    bucket_id TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


class QuotaWindowStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._journal_initialized = False
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._path), check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            if not self._journal_initialized:
                conn.execute("PRAGMA journal_mode=WAL;")
                self._journal_initialized = True
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.execute("PRAGMA foreign_keys=ON;")
            self._local.conn = conn
        return conn

    @contextmanager
    def _write(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                yield conn
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def _init_schema(self) -> None:
        conn = self._conn()
        conn.executescript(_DDL)
        with self._write() as tx:
            row = tx.execute("SELECT MAX(version) FROM schema_version").fetchone()
            current = row[0] or 0
            if current < _CURRENT_VERSION:
                tx.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                    (_CURRENT_VERSION, utc_iso(utc_now())),
                )

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def upsert_principal(self, principal: QuotaPrincipal) -> None:
        now = utc_iso(utc_now())
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO principals(provider, principal_hash, label, authentication_mode, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, principal_hash) DO UPDATE SET
                    label = excluded.label,
                    authentication_mode = excluded.authentication_mode,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    principal.provider,
                    principal.principal_hash,
                    principal.label,
                    principal.authentication_mode,
                    now,
                    now,
                ),
            )

    def upsert_bucket(self, bucket: QuotaBucket, principal_hash: str) -> None:
        now = utc_iso(utc_now())
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO buckets(
                    provider, principal_hash, bucket_id, bucket_name, window_semantics,
                    telemetry_quality, window_duration_seconds, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, principal_hash, bucket_id) DO UPDATE SET
                    bucket_name = excluded.bucket_name,
                    window_semantics = excluded.window_semantics,
                    telemetry_quality = excluded.telemetry_quality,
                    window_duration_seconds = excluded.window_duration_seconds,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    bucket.provider,
                    principal_hash,
                    bucket.bucket_id,
                    bucket.bucket_name,
                    bucket.window_semantics.value,
                    bucket.telemetry_quality.value,
                    bucket.window_duration_seconds,
                    now,
                    now,
                ),
            )

    def insert_snapshot(self, snapshot: QuotaSnapshot) -> bool:
        snap = QuotaSnapshot(
            provider=snapshot.provider,
            bucket_id=snapshot.bucket_id,
            principal_hash=snapshot.principal_hash,
            observed_at=normalize_utc(snapshot.observed_at) or utc_now(),
            telemetry_quality=snapshot.telemetry_quality,
            used_percent=snapshot.used_percent,
            reset_at=normalize_utc(snapshot.reset_at),
            limit_reached=snapshot.limit_reached,
            window_duration_seconds=snapshot.window_duration_seconds,
            raw_status=snapshot.raw_status,
            unavailable_reason=snapshot.unavailable_reason,
        )
        snapshot_id = _snapshot_identity(snap)
        with self._write() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO snapshots(
                    snapshot_id, provider, principal_hash, bucket_id, observed_at,
                    telemetry_quality, used_percent, reset_at, limit_reached,
                    window_duration_seconds, raw_status, unavailable_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    snap.provider,
                    snap.principal_hash,
                    snap.bucket_id,
                    utc_iso(snap.observed_at),
                    snap.telemetry_quality.value,
                    snap.used_percent,
                    utc_iso(snap.reset_at) or None,
                    None if snap.limit_reached is None else int(snap.limit_reached),
                    snap.window_duration_seconds,
                    snap.raw_status,
                    snap.unavailable_reason,
                    utc_iso(utc_now()),
                ),
            )
            return cur.rowcount > 0

    def set_adapter_status(self, status: QuotaAdapterStatus) -> None:
        checked = utc_iso(status.last_checked_at or utc_now())
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO adapter_status(
                    provider, enabled, status, reason, adapter_version, schema_version, last_checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    enabled = excluded.enabled,
                    status = excluded.status,
                    reason = excluded.reason,
                    adapter_version = excluded.adapter_version,
                    schema_version = excluded.schema_version,
                    last_checked_at = excluded.last_checked_at
                """,
                (
                    status.provider,
                    int(status.enabled),
                    status.status,
                    status.reason,
                    status.adapter_version,
                    status.schema_version,
                    checked,
                ),
            )

    def add_event(self, name: str, *, provider: str = "", principal_hash: str = "", bucket_id: str = "", reason: str = "", payload: Optional[dict] = None) -> None:
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO coordinator_events(event_name, provider, principal_hash, bucket_id, reason, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    provider,
                    principal_hash,
                    bucket_id,
                    reason,
                    json.dumps(payload or {}, sort_keys=True),
                    utc_iso(utc_now()),
                ),
            )

    def latest_snapshot(self, provider: str, principal_hash: str, bucket_id: str) -> Optional[dict]:
        row = self._conn().execute(
            """
            SELECT * FROM snapshots
            WHERE provider = ? AND principal_hash = ? AND bucket_id = ?
            ORDER BY observed_at DESC, created_at DESC
            LIMIT 1
            """,
            (provider, principal_hash, bucket_id),
        ).fetchone()
        return dict(row) if row else None

    def latest_usable_snapshot(self, provider: str, principal_hash: str, bucket_id: str) -> Optional[dict]:
        row = self._conn().execute(
            """
            SELECT * FROM snapshots
            WHERE provider = ?
              AND principal_hash = ?
              AND bucket_id = ?
              AND used_percent IS NOT NULL
              AND reset_at IS NOT NULL
              AND telemetry_quality IN ('authoritative', 'partial')
            ORDER BY observed_at DESC, created_at DESC
            LIMIT 1
            """,
            (provider, principal_hash, bucket_id),
        ).fetchone()
        return dict(row) if row else None

    def status(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        conn = self._conn()
        adapters = [dict(r) for r in conn.execute("SELECT * FROM adapter_status ORDER BY provider").fetchall()]
        buckets = [dict(r) for r in conn.execute("SELECT * FROM buckets ORDER BY provider, bucket_id").fetchall()]
        snapshots = [
            dict(r)
            for r in conn.execute(
                """
                SELECT s.* FROM snapshots s
                WHERE s.snapshot_id = (
                    SELECT i.snapshot_id
                    FROM snapshots i
                    WHERE i.provider = s.provider
                      AND i.principal_hash = s.principal_hash
                      AND i.bucket_id = s.bucket_id
                    ORDER BY i.observed_at DESC, i.created_at DESC, i.snapshot_id DESC
                    LIMIT 1
                )
                ORDER BY s.provider, s.bucket_id
                """
            ).fetchall()
        ]
        window_states = _build_window_states(conn, buckets=buckets, snapshots=snapshots, now=now or utc_now())
        return {"adapters": adapters, "buckets": buckets, "latest_snapshots": snapshots, "window_states": window_states}


def _row_key(row: dict) -> tuple[str, str, str]:
    return (str(row.get("provider") or ""), str(row.get("principal_hash") or ""), str(row.get("bucket_id") or ""))


def _reset_history(conn: sqlite3.Connection, *, provider: str, principal_hash: str, bucket_id: str, reset_at: str | None) -> dict:
    rows = conn.execute(
        """
        SELECT reset_at, observed_at, used_percent
        FROM snapshots
        WHERE provider = ?
          AND principal_hash = ?
          AND bucket_id = ?
          AND reset_at IS NOT NULL
        ORDER BY observed_at ASC, created_at ASC
        """,
        (provider, principal_hash, bucket_id),
    ).fetchall()
    reset_windows: dict[str, dict] = {}
    for row in rows:
        key = str(row["reset_at"])
        item = reset_windows.setdefault(
            key,
            {
                "reset_at": key,
                "first_seen_at": row["observed_at"],
                "last_seen_at": row["observed_at"],
                "first_used_percent": row["used_percent"],
                "last_used_percent": row["used_percent"],
            },
        )
        item["last_seen_at"] = row["observed_at"]
        item["last_used_percent"] = row["used_percent"]
    history = sorted(reset_windows.values(), key=lambda item: (str(item["last_seen_at"]), str(item["reset_at"])), reverse=True)
    current_since = None
    for item in history:
        if item["reset_at"] == reset_at:
            current_since = item["first_seen_at"]
            break
    observed_count = len(history)
    return {
        "observed_reset_count": observed_count,
        "current_reset_observed_since": current_since,
        "last_reset_change_at": history[0]["first_seen_at"] if observed_count > 1 else None,
        "reset_boundary_evidence": history,
    }


def _window_state_for(bucket: Optional[dict], snapshot: dict, *, now: datetime, history: dict) -> dict:
    provider = str(snapshot.get("provider") or (bucket.get("provider") if bucket else ""))
    principal_hash = str(snapshot.get("principal_hash") or (bucket.get("principal_hash") if bucket else ""))
    bucket_id = str(snapshot.get("bucket_id") or (bucket.get("bucket_id") if bucket else ""))
    quality = str(snapshot.get("telemetry_quality") or (bucket.get("telemetry_quality") if bucket else TelemetryQuality.UNAVAILABLE.value))
    semantics = str(bucket.get("window_semantics") if bucket else WindowSemantics.UNKNOWN.value)
    reset_at = normalize_utc(snapshot.get("reset_at"))
    observed_at = normalize_utc(snapshot.get("observed_at"))
    used_percent = snapshot.get("used_percent")
    reason = str(snapshot.get("unavailable_reason") or "")
    duration = snapshot.get("window_duration_seconds") or (bucket.get("window_duration_seconds") if bucket else None)
    active_session_state = "unknown"
    blockers: list[str] = []

    observation_stale = observed_at is not None and now - observed_at > _SNAPSHOT_STALE_AFTER

    if quality == TelemetryQuality.UNSUPPORTED.value:
        telemetry_state = "unsupported"
        blockers.append("telemetry_unsupported")
    elif quality in (TelemetryQuality.UNAVAILABLE.value, TelemetryQuality.MALFORMED.value):
        telemetry_state = quality
        blockers.append(f"telemetry_{quality}")
    elif observation_stale:
        telemetry_state = "stale"
        blockers.append("observation_stale")
    elif reason == "reset_at_stale" or reason.startswith("claude_get_usage_stale_after_") or _is_stale_reset(reset_at, now):
        telemetry_state = "stale"
        blockers.append(reason or "reset_at_stale")
    elif reset_at is None or used_percent is None:
        telemetry_state = "partial"
        blockers.append("telemetry_incomplete")
    else:
        telemetry_state = "current"

    if telemetry_state != "current" and "telemetry_not_current" not in blockers:
        blockers.append("telemetry_not_current")
    if quality != TelemetryQuality.AUTHORITATIVE.value:
        blockers.append("telemetry_not_authoritative")
    if semantics == WindowSemantics.UNKNOWN.value:
        blockers.append("window_semantics_unclassified")
    elif semantics != WindowSemantics.ANCHORED.value:
        blockers.append(f"window_semantics_{semantics}")
    if not principal_hash or principal_hash.endswith(":unknown"):
        blockers.append("principal_unknown")
    if active_session_state == "unknown":
        blockers.append("active_session_state_unknown")
    elif active_session_state == "true":
        blockers.append("active_session_active")

    window_start_inferred_at = None
    if semantics == WindowSemantics.ANCHORED.value and reset_at is not None and isinstance(duration, int):
        window_start_inferred_at = utc_iso(reset_at - timedelta(seconds=duration))

    classification_status = _classification_status(semantics=semantics, observed_reset_count=int(history.get("observed_reset_count") or 0))
    unique_blockers = list(dict.fromkeys(blockers))
    window_known = (
        telemetry_state == "current"
        and quality == TelemetryQuality.AUTHORITATIVE.value
        and semantics == WindowSemantics.ANCHORED.value
        and window_start_inferred_at is not None
        and reset_at is not None
    )
    automation_ready = (
        telemetry_state == "current"
        and quality == TelemetryQuality.AUTHORITATIVE.value
        and semantics == WindowSemantics.ANCHORED.value
        and principal_hash != ""
        and active_session_state == "false"
        and not unique_blockers
    )
    return {
        "provider": provider,
        "principal_hash": principal_hash,
        "bucket_id": bucket_id,
        "telemetry_state": telemetry_state,
        "telemetry_quality": quality,
        "window_semantics": semantics,
        "classification_status": classification_status,
        "used_percent": used_percent,
        "observed_at": utc_iso(observed_at) or None,
        "window_start_at": window_start_inferred_at,
        "window_start_inferred_at": window_start_inferred_at,
        "window_start_source": "inferred_from_reset_duration" if window_start_inferred_at else "unknown",
        "window_end_at": utc_iso(reset_at) or None,
        "active_session_state": active_session_state,
        "window_known": window_known,
        "automation_ready": automation_ready,
        "blockers": unique_blockers,
        **history,
    }


def _classification_status(*, semantics: str, observed_reset_count: int) -> str:
    if semantics == WindowSemantics.ANCHORED.value:
        return "proven_anchored"
    if semantics == WindowSemantics.FIXED.value:
        return "proven_fixed"
    if semantics == WindowSemantics.SLIDING.value:
        return "proven_sliding"
    if observed_reset_count >= 3:
        return "eligible_for_manual_probe"
    if observed_reset_count > 0:
        return "collecting"
    return "unknown"


def _build_window_states(conn: sqlite3.Connection, *, buckets: list[dict], snapshots: list[dict], now: datetime) -> list[dict]:
    bucket_by_key = {_row_key(bucket): bucket for bucket in buckets}
    states: list[dict] = []
    for snapshot in snapshots:
        key = _row_key(snapshot)
        bucket = bucket_by_key.get(key)
        reset_at = snapshot.get("reset_at")
        history = _reset_history(
            conn,
            provider=key[0],
            principal_hash=key[1],
            bucket_id=key[2],
            reset_at=str(reset_at) if reset_at else None,
        )
        states.append(_window_state_for(bucket, snapshot, now=now, history=history))
    return states


class UnsupportedQuotaAdapter:
    def __init__(self, provider: str, reason: str, *, adapter_version: str = "phase1-placeholder", schema_version: str = "unsupported") -> None:
        self.provider = provider
        self.reason = reason
        self.adapter_version = adapter_version
        self.schema_version = schema_version

    async def identify_principal(self) -> QuotaPrincipal:
        return QuotaPrincipal(provider=self.provider, principal_hash=f"{self.provider}:unknown", label=self.provider)

    async def discover_buckets(self) -> list[QuotaBucket]:
        return [
            QuotaBucket(
                provider=self.provider,
                bucket_id=f"{self.provider}/unsupported",
                bucket_name="Unsupported telemetry",
                telemetry_quality=TelemetryQuality.UNSUPPORTED,
            )
        ]

    async def observe(self, bucket_id: str) -> QuotaSnapshot:
        return QuotaSnapshot(
            provider=self.provider,
            principal_hash=f"{self.provider}:unknown",
            bucket_id=bucket_id,
            observed_at=utc_now(),
            telemetry_quality=TelemetryQuality.UNSUPPORTED,
            raw_status="unavailable",
            unavailable_reason=self.reason,
        )

    async def detect_active_user_session(self) -> bool | None:
        return None

    async def capabilities(self) -> AdapterCapability:
        return AdapterCapability(
            provider=self.provider,
            adapter_version=self.adapter_version,
            schema_version=self.schema_version,
            can_observe=False,
            telemetry_quality=TelemetryQuality.UNSUPPORTED,
            notes=self.reason,
        )


class ClaudeGetUsageQuotaAdapter:
    """Canonical Claude subscription quota reader via SDK ``get_usage`` control request."""

    provider = "claude"
    adapter_version = "claude-get-usage-v1"
    schema_version = "claude-get-usage-rate-limits-v1"

    def __init__(
        self,
        *,
        principal_key: str = "",
        cwd: str | Path | None = None,
        cli_path: str | Path | None = None,
        timeout_sec: float = 60.0,
        cache_ttl_sec: float = 60.0,
        now: Callable[[], datetime] = utc_now,
        read_usage: Optional[Callable[[], Any]] = None,
        claude_code_version_value: str | None = None,
    ) -> None:
        self.principal_key = principal_key.strip()
        self.cwd = Path(cwd).resolve() if cwd is not None else None
        self.cli_path = Path(cli_path) if cli_path is not None else None
        self.timeout_sec = max(1.0, float(timeout_sec))
        self.cache_ttl_sec = max(1.0, float(cache_ttl_sec))
        self._now = now
        self._read_usage = read_usage
        self._claude_code_version_value = claude_code_version_value
        self._cached_snapshot: Any | None = None
        self._cached_snapshot_at: float = 0.0
        self._last_successful_snapshot: Any | None = None
        self.model_invocations = 0

    async def identify_principal(self) -> QuotaPrincipal:
        key = self.principal_key or os.getenv("CLAUDE_QUOTA_PRINCIPAL_KEY", "").strip()
        cached = await self._read_snapshot()
        subscription = getattr(cached, "subscription_type", None)
        label = key or (f"claude-{subscription}" if subscription else "principal_unknown")
        return QuotaPrincipal(
            provider=self.provider,
            principal_hash=_principal_hash(self.provider, key or label),
            label=label,
            authentication_mode="claude_code_get_usage",
        )

    async def discover_buckets(self) -> list[QuotaBucket]:
        principal = await self.identify_principal()
        snapshot = await self._read_snapshot()
        buckets: list[QuotaBucket] = [
            QuotaBucket(
                provider=self.provider,
                bucket_id="five_hour",
                bucket_name="Claude 5-hour session limit",
                principal_hash=principal.principal_hash,
                window_semantics=WindowSemantics.ANCHORED,
                telemetry_quality=TelemetryQuality.AUTHORITATIVE,
                window_duration_seconds=5 * 60 * 60,
            ),
            QuotaBucket(
                provider=self.provider,
                bucket_id="seven_day",
                bucket_name="Claude 7-day rolling limit",
                principal_hash=principal.principal_hash,
                window_semantics=WindowSemantics.ANCHORED,
                telemetry_quality=TelemetryQuality.AUTHORITATIVE,
                window_duration_seconds=7 * 24 * 60 * 60,
            ),
        ]
        for limit in getattr(snapshot, "scoped_limits", []):
            if getattr(limit, "kind", None) != "weekly_scoped":
                continue
            display = getattr(limit, "model_display_name", None) or "scoped"
            bucket_id = f"weekly_scoped:{_slug(display)}"
            buckets.append(
                QuotaBucket(
                    provider=self.provider,
                    bucket_id=bucket_id,
                    bucket_name=f"Claude weekly scoped limit: {display}",
                    principal_hash=principal.principal_hash,
                    window_semantics=WindowSemantics.ANCHORED,
                    telemetry_quality=TelemetryQuality.AUTHORITATIVE,
                    window_duration_seconds=7 * 24 * 60 * 60,
                )
            )
        return buckets

    async def observe(self, bucket_id: str) -> QuotaSnapshot:
        principal = await self.identify_principal()
        snapshot = await self._read_snapshot()
        snapshot_status = getattr(snapshot, "status", None)
        if snapshot_status not in ("valid", "stale"):
            return QuotaSnapshot(
                provider=self.provider,
                principal_hash=principal.principal_hash,
                bucket_id=bucket_id,
                observed_at=getattr(snapshot, "observed_at", None) or self._now(),
                telemetry_quality=TelemetryQuality.UNAVAILABLE,
                raw_status=self._raw_status(snapshot),
                unavailable_reason=getattr(snapshot, "unavailable_reason", "") or "claude_get_usage_unavailable",
            )

        used_percent: float | None
        reset_at: datetime | None
        duration: int | None
        if bucket_id == "five_hour":
            used_percent = getattr(snapshot, "five_hour_utilization", None)
            reset_at = getattr(snapshot, "five_hour_resets_at", None)
            duration = 5 * 60 * 60
        elif bucket_id == "seven_day":
            used_percent = getattr(snapshot, "seven_day_utilization", None)
            reset_at = getattr(snapshot, "seven_day_resets_at", None)
            duration = 7 * 24 * 60 * 60
        elif bucket_id.startswith("weekly_scoped:"):
            limit = self._scoped_limit(snapshot, bucket_id)
            used_percent = getattr(limit, "percent", None) if limit is not None else None
            reset_at = getattr(limit, "resets_at", None) if limit is not None else None
            duration = 7 * 24 * 60 * 60
        else:
            used_percent = None
            reset_at = None
            duration = None

        reason = ""
        quality = TelemetryQuality.AUTHORITATIVE
        if snapshot_status == "stale":
            quality = TelemetryQuality.PARTIAL
            reason = getattr(snapshot, "unavailable_reason", "") or "claude_get_usage_stale_after_failure"
        if used_percent is None or reset_at is None:
            quality = TelemetryQuality.PARTIAL
            reason = "quota_bucket_incomplete"
        if _is_stale_reset(reset_at, getattr(snapshot, "observed_at", self._now())):
            reset_at = None
            quality = TelemetryQuality.PARTIAL
            reason = "reset_at_stale"
        return QuotaSnapshot(
            provider=self.provider,
            principal_hash=principal.principal_hash,
            bucket_id=bucket_id,
            observed_at=getattr(snapshot, "observed_at", None) or self._now(),
            telemetry_quality=quality,
            used_percent=used_percent,
            reset_at=reset_at,
            limit_reached=None if used_percent is None else used_percent >= 100.0,
            window_duration_seconds=duration,
            raw_status=self._raw_status(snapshot),
            unavailable_reason=reason,
        )

    async def detect_active_user_session(self) -> bool | None:
        return None

    async def capabilities(self) -> AdapterCapability:
        return AdapterCapability(
            provider=self.provider,
            adapter_version=self.adapter_version,
            schema_version=self.schema_version,
            can_observe=True,
            supports_active_session_detection=False,
            telemetry_quality=TelemetryQuality.AUTHORITATIVE,
            notes="canonical server-backed Claude Code get_usage control request",
        )

    async def _read_snapshot(self) -> Any:
        now = time.monotonic()
        if self._cached_snapshot is not None and now - self._cached_snapshot_at < self.cache_ttl_sec:
            return self._cached_snapshot
        try:
            from src.services.claude_usage_control import (
                claude_code_version,
                normalize_claude_quota,
                read_claude_usage_raw_with_new_client,
            )

            if self._read_usage is not None:
                raw = await self._read_usage()
            else:
                raw = await read_claude_usage_raw_with_new_client(
                    cwd=self.cwd,
                    cli_path=self.cli_path,
                    timeout=self.timeout_sec,
                )
            version_value = self._claude_code_version_value or claude_code_version(
                cli_path=self.cli_path,
                timeout_sec=2.0,
            )
            snapshot = normalize_claude_quota(
                raw,
                observed_at=self._now(),
                claude_version=version_value,
            )
            if getattr(snapshot, "status", None) == "valid":
                self._last_successful_snapshot = snapshot
            elif self._last_successful_snapshot is not None:
                snapshot = self._last_successful_snapshot.model_copy(
                    update={
                        "status": "stale",
                        "unavailable_reason": f"claude_get_usage_stale_after_unavailable:{getattr(snapshot, 'unavailable_reason', '') or 'empty_rate_limits'}",
                    }
                )
        except Exception as exc:
            from src.services.claude_usage_control import ClaudeQuotaSnapshot

            reason = f"claude_get_usage_failed:{type(exc).__name__}"
            if self._last_successful_snapshot is not None:
                snapshot = self._last_successful_snapshot.model_copy(
                    update={
                        "status": "stale",
                        "unavailable_reason": f"claude_get_usage_stale_after_failure:{reason}",
                    }
                )
            else:
                snapshot = ClaudeQuotaSnapshot(
                    status="unavailable",
                    observed_at=self._now(),
                    unavailable_reason=reason,
                )
        self._cached_snapshot = snapshot
        self._cached_snapshot_at = now
        return snapshot

    def _scoped_limit(self, snapshot: Any, bucket_id: str) -> Any | None:
        for limit in getattr(snapshot, "scoped_limits", []):
            display = getattr(limit, "model_display_name", None) or "scoped"
            if bucket_id == f"weekly_scoped:{_slug(display)}":
                return limit
        return None

    def _raw_status(self, snapshot: Any) -> str:
        sdk = getattr(snapshot, "sdk_version", None) or "unknown"
        claude_code = getattr(snapshot, "claude_code_version", None) or "unknown"
        return f"claude_get_usage sdk={sdk} claude_code={claude_code}"


def _slug(value: str) -> str:
    chars: list[str] = []
    for ch in value.lower():
        chars.append(ch if ch.isalnum() else "_")
    return "_".join(part for part in "".join(chars).split("_") if part) or "scoped"


class FakeQuotaAdapter:
    """Deterministic test adapter; no method sends a model request."""

    def __init__(
        self,
        *,
        provider: str = "fake",
        principal_hash: str = "fake-principal",
        buckets: Optional[list[QuotaBucket]] = None,
        snapshots: Optional[Dict[str, list[QuotaSnapshot]]] = None,
        capability: Optional[AdapterCapability] = None,
        malformed: bool = False,
        active_user_session: bool | None = None,
    ) -> None:
        self.provider = provider
        self.principal_hash = principal_hash
        self._buckets = buckets or [
            QuotaBucket(
                provider=provider,
                bucket_id="five-hour",
                bucket_name="Five hour",
                principal_hash=principal_hash,
                telemetry_quality=TelemetryQuality.AUTHORITATIVE,
            )
        ]
        self._snapshots = snapshots or {}
        self._indexes: Dict[str, int] = {}
        self._capability = capability or AdapterCapability(
            provider=provider,
            adapter_version="fake-1",
            schema_version="quota-v1",
            can_observe=True,
            supports_active_session_detection=True,
            telemetry_quality=TelemetryQuality.AUTHORITATIVE,
        )
        self.malformed = malformed
        self.active_user_session = active_user_session
        self.model_invocations = 0

    async def identify_principal(self) -> QuotaPrincipal:
        return QuotaPrincipal(provider=self.provider, principal_hash=self.principal_hash, label="fake", authentication_mode="test")

    async def discover_buckets(self) -> list[QuotaBucket]:
        return self._buckets

    async def observe(self, bucket_id: str) -> QuotaSnapshot:
        if self.malformed:
            raise QuotaAdapterError("malformed_provider_response", quality=TelemetryQuality.MALFORMED)
        seq = self._snapshots.get(bucket_id)
        if seq:
            idx = self._indexes.get(bucket_id, 0)
            snapshot = seq[min(idx, len(seq) - 1)]
            self._indexes[bucket_id] = idx + 1
            return snapshot
        return QuotaSnapshot(
            provider=self.provider,
            principal_hash=self.principal_hash,
            bucket_id=bucket_id,
            observed_at=utc_now(),
            telemetry_quality=TelemetryQuality.UNAVAILABLE,
            raw_status="unavailable",
            unavailable_reason="fake_no_snapshot",
        )

    async def detect_active_user_session(self) -> bool | None:
        return self.active_user_session

    async def capabilities(self) -> AdapterCapability:
        return self._capability


class QuotaWindowCoordinator:
    def __init__(
        self,
        *,
        store: QuotaWindowStore,
        adapters: Iterable[QuotaAdapter],
        enabled: bool = False,
        observe_interval_sec: int = 300,
        observe_max_interval_sec: int = 21600,
        reset_probe_lead_sec: int = 900,
        expected_schema_versions: Optional[Dict[str, str]] = None,
        now: Callable[[], datetime] = utc_now,
        event_handlers: Optional[Iterable[QuotaEventHandler]] = None,
    ) -> None:
        self.store = store
        self.adapters = list(adapters)
        self.enabled = enabled
        self.observe_interval_sec = max(30, int(observe_interval_sec))
        self.observe_max_interval_sec = max(self.observe_interval_sec, int(observe_max_interval_sec))
        self.reset_probe_lead_sec = max(0, int(reset_probe_lead_sec))
        self.expected_schema_versions = expected_schema_versions or {}
        self._now = now
        self._event_handlers = list(event_handlers or [])
        self._task: Optional[asyncio.Task] = None
        self._last_now: Optional[datetime] = None

    async def start(self) -> None:
        if not self.enabled:
            logger.info("event=quota_coordinator_disabled")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._observe_loop())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def read_status(self) -> Dict[str, Any]:
        data = self.store.status(now=self._now())
        data["enabled"] = self.enabled
        data["mode"] = "observe_only"
        return data

    async def observe_once(self) -> None:
        self._record_clock_state()
        for adapter in self.adapters:
            await self._observe_adapter(adapter)

    async def _observe_loop(self) -> None:
        try:
            while True:
                await self.observe_once()
                await asyncio.sleep(self.next_observe_delay_sec())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("quota_observe_loop_stopped err=%s", e)

    def next_observe_delay_sec(self) -> int:
        now = normalize_utc(self._now()) or utc_now()
        snapshots = self.store.status().get("latest_snapshots", [])
        known_reset_delays: list[int] = []
        for row in snapshots:
            if row.get("limit_reached") == 1:
                return self.observe_interval_sec
            reset_at = normalize_utc(row.get("reset_at"))
            used_percent = row.get("used_percent")
            quality = row.get("telemetry_quality")
            if reset_at is None or used_percent is None:
                continue
            if quality not in (TelemetryQuality.AUTHORITATIVE.value, TelemetryQuality.PARTIAL.value):
                continue
            seconds_until_probe = int((reset_at - now).total_seconds()) - self.reset_probe_lead_sec
            if seconds_until_probe > self.observe_interval_sec:
                known_reset_delays.append(seconds_until_probe)
        if not known_reset_delays:
            return self.observe_interval_sec
        return max(self.observe_interval_sec, min(min(known_reset_delays), self.observe_max_interval_sec))

    def _record_clock_state(self) -> None:
        now = normalize_utc(self._now()) or utc_now()
        if self._last_now is not None and now < self._last_now:
            self._record_event("quota.clock_rollback", reason="clock_rollback", payload={"previous": utc_iso(self._last_now), "current": utc_iso(now)})
        self._last_now = now

    async def _observe_adapter(self, adapter: QuotaAdapter) -> None:
        cap = await adapter.capabilities()
        expected = self.expected_schema_versions.get(cap.provider)
        if expected and cap.schema_version != expected:
            status = QuotaAdapterStatus(
                provider=cap.provider,
                enabled=False,
                status="disabled",
                reason="version_mismatch",
                adapter_version=cap.adapter_version,
                schema_version=cap.schema_version,
                last_checked_at=self._now(),
            )
            self.store.set_adapter_status(status)
            self._record_event("adapter.disabled_version", provider=cap.provider, reason="version_mismatch", payload=_safe_dict(cap))
            return

        if not cap.can_observe:
            status = QuotaAdapterStatus(
                provider=cap.provider,
                enabled=False,
                status="unavailable",
                reason=cap.notes or "telemetry_unavailable",
                adapter_version=cap.adapter_version,
                schema_version=cap.schema_version,
                last_checked_at=self._now(),
            )
            self.store.set_adapter_status(status)
            self._record_event("quota.adapter_unavailable", provider=cap.provider, reason=status.reason, payload=_safe_dict(cap))
            try:
                principal = await adapter.identify_principal()
                self.store.upsert_principal(principal)
                for bucket in await adapter.discover_buckets():
                    self.store.upsert_bucket(bucket, principal.principal_hash)
                    snapshot = await adapter.observe(bucket.bucket_id)
                    self.store.insert_snapshot(snapshot)
            except Exception:
                pass
            return

        self.store.set_adapter_status(
            QuotaAdapterStatus(
                provider=cap.provider,
                enabled=True,
                status="ready",
                adapter_version=cap.adapter_version,
                schema_version=cap.schema_version,
                last_checked_at=self._now(),
            )
        )
        principal = await adapter.identify_principal()
        self.store.upsert_principal(principal)
        active = await adapter.detect_active_user_session()
        active_label = "unknown" if active is None else str(bool(active)).lower()
        for bucket in await adapter.discover_buckets():
            self.store.upsert_bucket(bucket, principal.principal_hash)
            try:
                snapshot = await adapter.observe(bucket.bucket_id)
            except QuotaAdapterError as e:
                snapshot = QuotaSnapshot(
                    provider=cap.provider,
                    principal_hash=principal.principal_hash,
                    bucket_id=bucket.bucket_id,
                    observed_at=self._now(),
                    telemetry_quality=e.quality,
                    raw_status="unavailable",
                    unavailable_reason=e.reason,
                )
            snapshot = self._retain_previous_success_as_stale(snapshot)
            inserted = self.store.insert_snapshot(snapshot)
            event_name = "quota.observed" if inserted else "quota.duplicate_snapshot"
            self._record_event(
                event_name,
                provider=snapshot.provider,
                principal_hash=snapshot.principal_hash,
                bucket_id=snapshot.bucket_id,
                reason=snapshot.unavailable_reason,
                payload={
                    "bucket_name": bucket.bucket_name,
                    "used_percent": snapshot.used_percent,
                    "reset_at": utc_iso(snapshot.reset_at) or None,
                    "telemetry_quality": snapshot.telemetry_quality.value,
                    "window_semantics": bucket.window_semantics.value,
                    "active_user_session": active_label,
                },
            )

    def _retain_previous_success_as_stale(self, snapshot: QuotaSnapshot) -> QuotaSnapshot:
        if snapshot.telemetry_quality != TelemetryQuality.UNAVAILABLE:
            return snapshot
        if not snapshot.raw_status.startswith("claude_get_usage"):
            return snapshot
        previous = self.store.latest_usable_snapshot(
            snapshot.provider,
            snapshot.principal_hash,
            snapshot.bucket_id,
        )
        if previous is None:
            return snapshot
        observed_at = normalize_utc(previous.get("observed_at"))
        reset_at = normalize_utc(previous.get("reset_at"))
        used = previous.get("used_percent")
        if observed_at is None or reset_at is None or not isinstance(used, (int, float)):
            return snapshot
        reason = snapshot.unavailable_reason or "claude_get_usage_unavailable"
        return QuotaSnapshot(
            provider=snapshot.provider,
            principal_hash=snapshot.principal_hash,
            bucket_id=snapshot.bucket_id,
            observed_at=observed_at,
            telemetry_quality=TelemetryQuality.PARTIAL,
            used_percent=float(used),
            reset_at=reset_at,
            limit_reached=bool(previous.get("limit_reached")) if previous.get("limit_reached") is not None else None,
            window_duration_seconds=previous.get("window_duration_seconds"),
            raw_status=str(previous.get("raw_status") or snapshot.raw_status),
            unavailable_reason=f"claude_get_usage_stale_after_unavailable:{reason}",
        )

    def _record_event(self, name: str, *, provider: str = "", principal_hash: str = "", bucket_id: str = "", reason: str = "", payload: Optional[dict] = None) -> None:
        self.store.add_event(name, provider=provider, principal_hash=principal_hash, bucket_id=bucket_id, reason=reason, payload=payload)
        event_payload: Dict[str, Any] = {
            "provider": provider,
            "principal_hash": principal_hash,
            "bucket_id": bucket_id,
            "reason": reason,
            **(payload or {}),
        }
        for handler in self._event_handlers:
            try:
                handler(name, event_payload)
            except Exception:
                logger.debug("quota_event_handler_failed event=%s", name, exc_info=True)
        try:
            from src.core.observability import emit_event

            clean_payload = payload or {}
            emit_event(name, provider=provider, principal_hash=principal_hash, bucket_id=bucket_id, reason=reason, **clean_payload)
        except Exception:
            pass


def _safe_dict(value: Any) -> Dict[str, Any]:
    data = asdict(value)
    return {k: _enum_value(v) for k, v in data.items()}


def build_default_quota_adapters() -> list[QuotaAdapter]:
    from config import config

    quota_cfg = getattr(config, "quota", None)
    return [
        UnsupportedQuotaAdapter("codex", "codex_quota_telemetry_not_validated_phase1"),
        ClaudeGetUsageQuotaAdapter(
            principal_key=getattr(quota_cfg, "claude_principal_key", ""),
            cwd=getattr(getattr(config, "claude", None), "base_cwd", None),
            cli_path=getattr(getattr(config, "claude", None), "sdk_cli_path", None),
            timeout_sec=getattr(quota_cfg, "claude_get_usage_timeout_sec", 60.0),
        ),
        UnsupportedQuotaAdapter("opencode", "opencode_is_provider_router_no_phase1_quota_owner"),
    ]


def build_quota_coordinator_from_config(
    *,
    enabled: Optional[bool] = None,
    event_handlers: Optional[Iterable[QuotaEventHandler]] = None,
) -> QuotaWindowCoordinator:
    from config import config

    quota_cfg = getattr(config, "quota", None)
    db_path = getattr(quota_cfg, "db_path", "state/quota_windows.db")
    cfg_enabled = bool(getattr(quota_cfg, "enabled", False)) if enabled is None else bool(enabled)
    interval = int(getattr(quota_cfg, "observe_interval_sec", 300))
    max_interval = int(getattr(quota_cfg, "observe_max_interval_sec", 21600))
    lead = int(getattr(quota_cfg, "reset_probe_lead_sec", 900))
    return QuotaWindowCoordinator(
        store=QuotaWindowStore(db_path),
        adapters=build_default_quota_adapters(),
        enabled=cfg_enabled,
        observe_interval_sec=interval,
        observe_max_interval_sec=max_interval,
        reset_probe_lead_sec=lead,
        event_handlers=event_handlers,
    )
