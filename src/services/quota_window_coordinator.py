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
import shlex
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
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._path), check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
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

    def status(self) -> Dict[str, Any]:
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
        return {"adapters": adapters, "buckets": buckets, "latest_snapshots": snapshots}


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


class ClaudeStatusLineQuotaAdapter:
    """Observe Claude Code quota from captured status-line JSON.

    Claude Code passes status-line data to the configured statusLine command on
    stdin. This adapter reads the latest captured JSON (or an operator-provided
    read command) and never starts a Claude model turn.
    """

    provider = "claude"
    adapter_version = "claude-statusline-v1"
    schema_version = "claude-statusline-rate-limits-v1"

    def __init__(
        self,
        *,
        status_json_path: str | Path = "state/claude_statusline_latest.json",
        status_command: str = "",
        principal_key: str = "",
        timeout_sec: float = 2.0,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.status_json_path = Path(status_json_path)
        self.status_command = status_command.strip()
        self.principal_key = principal_key.strip()
        self.timeout_sec = max(0.1, float(timeout_sec))
        self._now = now
        self._cached_status: Optional[Dict[str, Any]] = None
        self._cached_status_at: float = 0.0
        self.model_invocations = 0

    async def identify_principal(self) -> QuotaPrincipal:
        key = self.principal_key or os.getenv("CLAUDE_QUOTA_PRINCIPAL_KEY", "").strip()
        label = key or "principal_unknown"
        return QuotaPrincipal(
            provider=self.provider,
            principal_hash=_principal_hash(self.provider, key),
            label=label,
            authentication_mode="claude_code_status_line",
        )

    async def discover_buckets(self) -> list[QuotaBucket]:
        principal = await self.identify_principal()
        return [
            QuotaBucket(
                provider=self.provider,
                bucket_id="five_hour",
                bucket_name="Claude 5-hour session limit",
                principal_hash=principal.principal_hash,
                telemetry_quality=TelemetryQuality.PARTIAL,
                window_duration_seconds=5 * 60 * 60,
            ),
            QuotaBucket(
                provider=self.provider,
                bucket_id="seven_day",
                bucket_name="Claude 7-day rolling limit",
                principal_hash=principal.principal_hash,
                telemetry_quality=TelemetryQuality.PARTIAL,
                window_duration_seconds=7 * 24 * 60 * 60,
            ),
        ]

    async def observe(self, bucket_id: str) -> QuotaSnapshot:
        principal = await self.identify_principal()
        try:
            status = await self._read_status_json()
        except QuotaAdapterError as e:
            return QuotaSnapshot(
                provider=self.provider,
                principal_hash=principal.principal_hash,
                bucket_id=bucket_id,
                observed_at=self._now(),
                telemetry_quality=e.quality,
                raw_status="unavailable",
                unavailable_reason=e.reason,
            )

        rate_limits = status.get("rate_limits") if isinstance(status, dict) else None
        bucket = rate_limits.get(bucket_id) if isinstance(rate_limits, dict) else None
        if not isinstance(bucket, dict):
            return QuotaSnapshot(
                provider=self.provider,
                principal_hash=principal.principal_hash,
                bucket_id=bucket_id,
                observed_at=self._now(),
                telemetry_quality=TelemetryQuality.UNAVAILABLE,
                raw_status="status_line",
                unavailable_reason="rate_limit_bucket_absent",
            )

        used_percent = _coerce_percent(bucket.get("used_percentage"))
        reset_at = _coerce_reset_at(bucket.get("resets_at"))
        observed_at = self._now()
        reason = ""
        if _is_stale_reset(reset_at, observed_at):
            reset_at = None
            reason = "reset_at_stale"
        quality = TelemetryQuality.AUTHORITATIVE if used_percent is not None and reset_at is not None else TelemetryQuality.PARTIAL
        limit_reached = None if used_percent is None else used_percent >= 100.0
        return QuotaSnapshot(
            provider=self.provider,
            principal_hash=principal.principal_hash,
            bucket_id=bucket_id,
            observed_at=observed_at,
            telemetry_quality=quality,
            used_percent=used_percent,
            reset_at=reset_at,
            limit_reached=limit_reached,
            window_duration_seconds=5 * 60 * 60 if bucket_id == "five_hour" else 7 * 24 * 60 * 60,
            raw_status="status_line",
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
            telemetry_quality=TelemetryQuality.PARTIAL,
            notes="reads captured Claude Code status-line JSON; no model request",
        )

    async def _read_status_json(self) -> Dict[str, Any]:
        now = time.monotonic()
        if self._cached_status is not None and now - self._cached_status_at < 1.0:
            return self._cached_status
        if self.status_command:
            data = await self._read_status_command()
            self._cached_status = data
            self._cached_status_at = now
            return data
        try:
            raw = self.status_json_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise QuotaAdapterError("claude_status_line_json_missing", quality=TelemetryQuality.UNAVAILABLE)
        except OSError:
            raise QuotaAdapterError("claude_status_line_json_unreadable", quality=TelemetryQuality.UNAVAILABLE)
        data = self._parse_status_json(raw)
        self._cached_status = data
        self._cached_status_at = now
        return data

    async def _read_status_command(self) -> Dict[str, Any]:
        args = shlex.split(self.status_command)
        if not args:
            raise QuotaAdapterError("claude_status_line_command_empty", quality=TelemetryQuality.UNAVAILABLE)
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_sec)
        except asyncio.TimeoutError:
            raise QuotaAdapterError("claude_status_line_command_timeout", quality=TelemetryQuality.UNAVAILABLE)
        except OSError:
            raise QuotaAdapterError("claude_status_line_command_failed", quality=TelemetryQuality.UNAVAILABLE)
        if proc.returncode != 0:
            raise QuotaAdapterError("claude_status_line_command_nonzero", quality=TelemetryQuality.UNAVAILABLE)
        return self._parse_status_json(stdout.decode("utf-8", errors="replace"))

    def _parse_status_json(self, raw: str) -> Dict[str, Any]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise QuotaAdapterError("claude_status_line_json_malformed", quality=TelemetryQuality.MALFORMED)
        if not isinstance(data, dict):
            raise QuotaAdapterError("claude_status_line_json_not_object", quality=TelemetryQuality.MALFORMED)
        return data


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
        data = self.store.status()
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
        ClaudeStatusLineQuotaAdapter(
            status_json_path=getattr(quota_cfg, "claude_status_json_path", "state/claude_statusline_latest.json"),
            status_command=getattr(quota_cfg, "claude_status_command", ""),
            principal_key=getattr(quota_cfg, "claude_principal_key", ""),
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
