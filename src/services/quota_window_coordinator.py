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
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Generator, Iterable, List, Optional, Protocol

logger = logging.getLogger(__name__)
_SNAPSHOT_STALE_AFTER = timedelta(minutes=15)

#: Grace added when sleeping to a SPENT window's reset instant: a read taken
#: exactly on the boundary still reports the closing window (observed live —
#: 17:20:00Z reset, first clean reading at 17:20:30Z).
_EXHAUSTED_SETTLE_SEC = 30

#: Grace added ON TOP of the observer's own next scheduled probe time when
#: deciding whether an observation counts as stale. The adaptive cadence
#: deliberately sleeps up to ``observe_max_interval_sec`` until just before
#: the next known reset (A78 §2), so a healthy reading is EXPECTED to be hours
#: old — labelling that "stale" told every consumer the instrument was blind
#: when it was merely operating on schedule. An observation is stale only once
#: the poll that should have replaced it is overdue.
_STALE_SCHEDULE_GRACE = timedelta(minutes=5)

#: ``reset_boundary_evidence`` bounding (A78 §4): the API response carries the
#: most recent boundaries only; full history stays in the store.
_EVIDENCE_LIMIT = 12
_EVIDENCE_SCAN_MAX_ROWS = 5000
#: The provider reports ``reset_at`` with sub-second jitter (live:
#: 21:59:59.523–22:00:00.48 all naming ONE weekly boundary), which used to
#: split a single window into dozens of evidence entries. Boundaries closer
#: than this are one window.
_BOUNDARY_JITTER = timedelta(seconds=2)

#: Retention default (A78 §4): redundant confirmation snapshots older than this
#: are pruned; the FIRST sighting of each boundary is always kept (it is the
#: anchored-window evidence), as is everything newer than the cutoff.
_SNAPSHOT_RETENTION_DAYS = 14

#: ``coordinator_events`` retention (A93). The table is write-only diagnostics:
#: no code path reads it back (the Telegram digest consumes events in memory via
#: ``event_handlers``), so the window only has to cover operator forensics. It
#: matches the snapshot window so every retained snapshot keeps its surrounding
#: events. ``<= 0`` disables event pruning.
_EVENT_RETENTION_DAYS = 14

#: Rows removed per DELETE, each in its own short write transaction (A93): a
#: first prune of a 225k-row backlog must not hold the SQLite write lock — the
#: controller store is shared by the gateway and the task-server — for long.
_PRUNE_BATCH_ROWS = 5000

#: Append-only quota history (A93, owner amendment on PR #22): every row the
#: retention pass removes is first archived here, next to ``quota_windows.db``.
#: Unlike the store this is NOT a cache — past ``used_percent`` series cannot be
#: re-observed — so it is never pruned.
_ARCHIVE_FILENAME = "quota_archive.db"


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
    #: A refusal the PROVIDER handed us (429 / rate_limit_event with its own
    #: ``resetsAt``) recorded as telemetry — event-derived, not observed by a
    #: poll. Exact at ``observed_at``, carries ``limit_reached`` (A78 §3).
    EVENT_DERIVED = "event_derived"


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
    #: The adapter's live "is an interactive user session active right now"
    #: reading at observation time ("true"/"false"/None=unknown). Threaded
    #: through to the read model so ``automation_ready`` is reachable (A78 §1)
    #: instead of dying as a local literal in ``_window_state_for``.
    active_session_state: Optional[str] = None


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
        snapshot.active_session_state or "",
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


_CURRENT_VERSION = 2

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
    active_session_state TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_bucket_observed
    ON snapshots(provider, principal_hash, bucket_id, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_snapshots_observed_at
    ON snapshots(observed_at);

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


#: Snapshots the retention pass may remove: older than the cutoff and not the
#: FIRST sighting of their window boundary. Anchors are computed in one pass via
#: ROW_NUMBER() and excluded; a per-row correlated EXISTS subquery here once
#: re-scanned each row's whole bucket per candidate (near-quadratic on this
#: table). Deleting non-anchor rows never changes which row is an anchor, so
#: recomputing it per batch is safe. Params: (cutoff, batch_rows).
#: A boundary is a cluster of ``reset_at`` instants within ``_BOUNDARY_JITTER``
#: of the previous one (sorted per bucket) — the same clustering the evidence
#: reader uses; the provider drifts sub-second, so exact strings made nearly
#: every row its own "boundary" and nothing old was ever pruned. NULL
#: ``reset_at`` rows form one boundary per bucket, as before.
_PRUNABLE_SNAPSHOT_IDS_SQL = f"""
    SELECT snapshot_id FROM snapshots WHERE observed_at < ?
      AND snapshot_id NOT IN (
        SELECT snapshot_id FROM (
          SELECT snapshot_id,
                 ROW_NUMBER() OVER (
                   PARTITION BY provider, principal_hash, bucket_id, boundary
                   ORDER BY observed_at ASC, created_at ASC
                 ) AS rn
          FROM (
            SELECT snapshot_id, provider, principal_hash, bucket_id, observed_at, created_at,
                   SUM(starts_boundary) OVER (
                     PARTITION BY provider, principal_hash, bucket_id
                     ORDER BY reset_ts, observed_at, created_at
                     ROWS UNBOUNDED PRECEDING
                   ) AS boundary
            FROM (
              SELECT *, CASE
                  WHEN prev_ts = -1 THEN 1
                  WHEN reset_ts IS NULL THEN 0
                  WHEN prev_ts IS NULL THEN 1
                  WHEN (reset_ts - prev_ts) * 86400.0 > {_BOUNDARY_JITTER.total_seconds()} THEN 1
                  ELSE 0
                END AS starts_boundary
              FROM (
                SELECT snapshot_id, provider, principal_hash, bucket_id, observed_at, created_at,
                       julianday(reset_at) AS reset_ts,
                       LAG(julianday(reset_at), 1, -1) OVER (
                         PARTITION BY provider, principal_hash, bucket_id
                         ORDER BY julianday(reset_at), observed_at, created_at
                       ) AS prev_ts
                FROM snapshots
              )
            )
          )
        )
        WHERE rn = 1
      )
    LIMIT ?
"""

_ARCHIVE_DDL = """
CREATE TABLE IF NOT EXISTS snapshots_archive (
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
    active_session_state TEXT,
    created_at TEXT NOT NULL,
    archived_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_archive_bucket_observed
    ON snapshots_archive(provider, principal_hash, bucket_id, observed_at);

CREATE TABLE IF NOT EXISTS event_runs (
    store_id TEXT NOT NULL,
    first_id INTEGER NOT NULL,
    last_id INTEGER NOT NULL,
    event_name TEXT NOT NULL,
    provider TEXT NOT NULL,
    principal_hash TEXT NOT NULL,
    bucket_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    first_at TEXT NOT NULL,
    last_at TEXT NOT NULL,
    count INTEGER NOT NULL,
    first_payload TEXT NOT NULL,
    last_payload TEXT NOT NULL,
    PRIMARY KEY (store_id, first_id)
);

CREATE INDEX IF NOT EXISTS idx_event_runs_store_last
    ON event_runs(store_id, last_id);

CREATE INDEX IF NOT EXISTS idx_event_runs_name_first_at
    ON event_runs(event_name, first_at);
"""

_SNAPSHOT_COLUMNS = (
    "snapshot_id", "provider", "principal_hash", "bucket_id", "observed_at",
    "telemetry_quality", "used_percent", "reset_at", "limit_reached",
    "window_duration_seconds", "raw_status", "unavailable_reason",
    "active_session_state", "created_at",
)


class QuotaArchiveError(RuntimeError):
    """Archiving a prune batch failed; that batch was NOT deleted (A93)."""


class QuotaStoreIdentityConflict(QuotaArchiveError):
    """A store row sits inside an id range the archive already holds for this
    store identity, but no archived run accounts for it — the store was rebuilt
    or restored from an older copy while its archive survived (A93)."""


class QuotaArchive:
    """Append-only quota history next to the store (A93).

    ``snapshots_archive`` keeps pruned snapshots at full fidelity, keyed on
    ``snapshot_id`` (idempotent). ``event_runs`` compacts pruned
    ``coordinator_events``: each run of consecutive (by ``id``) events with the
    same ``(event_name, provider, principal_hash, bucket_id, reason)`` is one
    row carrying its id range, first/last time, count and first/last payload —
    lossless for timing and counts, and a singleton (a refusal) keeps its
    payload. Runs are scoped by the store's identity (``store_id``): event ids
    are only meaningful within one store file. Short-lived connections: it is
    written once per prune batch."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @contextmanager
    def _write(self) -> Generator[sqlite3.Connection, None, None]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), isolation_level=None, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                # Switching modes is not covered by the busy timeout; WAL is
                # persistent, so losing this race to a concurrent opener is fine.
                try:
                    conn.execute("PRAGMA journal_mode=WAL;")
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower():
                        raise
            conn.executescript(_ARCHIVE_DDL)
            conn.execute("BEGIN IMMEDIATE;")
            try:
                yield conn
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        finally:
            conn.close()

    def archive_snapshots(self, rows: List[Dict[str, Any]]) -> None:
        archived_at = utc_iso(utc_now())
        cols = ", ".join(_SNAPSHOT_COLUMNS)
        marks = ", ".join("?" for _ in _SNAPSHOT_COLUMNS)
        with self._write() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO snapshots_archive({cols}, archived_at) VALUES ({marks}, ?)",
                [tuple(r.get(c) for c in _SNAPSHOT_COLUMNS) + (archived_at,) for r in rows],
            )

    def archive_events(self, rows: List[Dict[str, Any]], *, store_id: str) -> None:
        """Fold id-ordered ``rows`` from the store identified by ``store_id``
        into ``event_runs``. That store's watermark (``MAX(last_id)``) is read
        inside the write transaction, so a retried or concurrent pass never
        counts an event twice; the newest run is extended when the next event
        continues it.

        A row at or below the watermark is skipped ONLY when an archived run of
        this store covers it — same id range, same identity key, ``created_at``
        within the run's time span. Anything else means the store's ids were
        reused (rebuilt or restored store under a surviving archive): raise
        ``QuotaStoreIdentityConflict`` and write nothing, so no row is ever
        deleted without its archive copy."""

        def key(r: Dict[str, Any]) -> tuple:
            return (r["event_name"], r["provider"], r["principal_hash"], r["bucket_id"], r["reason"])

        with self._write() as conn:
            last = conn.execute(
                "SELECT * FROM event_runs WHERE store_id = ? ORDER BY last_id DESC LIMIT 1", (store_id,),
            ).fetchone()
            watermark = int(last["last_id"]) if last else 0
            current: Optional[Dict[str, Any]] = dict(last) if last else None
            is_new = False
            dirty: List[tuple[Dict[str, Any], bool]] = []
            for r in sorted(rows, key=lambda row: row["id"]):
                if int(r["id"]) <= watermark:
                    covering = conn.execute(
                        "SELECT * FROM event_runs WHERE store_id = ? AND first_id <= ? AND last_id >= ?",
                        (store_id, r["id"], r["id"]),
                    ).fetchone()
                    if (
                        covering is None
                        or key(dict(covering)) != key(r)
                        or not (covering["first_at"] <= r["created_at"] <= covering["last_at"])
                    ):
                        raise QuotaStoreIdentityConflict(
                            f"store {store_id} event id {r['id']} is at or below the archive watermark "
                            f"{watermark} but no archived run accounts for it"
                        )
                    continue
                if current is not None and key(current) == key(r):
                    current.update(last_id=r["id"], last_at=r["created_at"], last_payload=r["payload"])
                    current["count"] = int(current["count"]) + 1
                else:
                    if current is not None:
                        dirty.append((current, is_new))
                    current = {
                        "store_id": store_id,
                        "first_id": r["id"], "last_id": r["id"], "event_name": r["event_name"],
                        "provider": r["provider"], "principal_hash": r["principal_hash"],
                        "bucket_id": r["bucket_id"], "reason": r["reason"],
                        "first_at": r["created_at"], "last_at": r["created_at"], "count": 1,
                        "first_payload": r["payload"], "last_payload": r["payload"],
                    }
                    is_new = True
            if current is not None:
                dirty.append((current, is_new))
            for run, new in dirty:
                if new:
                    conn.execute(
                        "INSERT INTO event_runs(store_id, first_id, last_id, event_name, provider,"
                        " principal_hash, bucket_id, reason, first_at, last_at, count, first_payload,"
                        " last_payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (store_id, run["first_id"], run["last_id"], run["event_name"], run["provider"],
                         run["principal_hash"], run["bucket_id"], run["reason"], run["first_at"],
                         run["last_at"], run["count"], run["first_payload"],
                         # A singleton's payload lives in first_payload only.
                         run["last_payload"] if int(run["count"]) > 1 else ""),
                    )
                else:
                    conn.execute(
                        "UPDATE event_runs SET last_id = ?, last_at = ?, count = ?, last_payload = ?"
                        " WHERE store_id = ? AND first_id = ?",
                        (run["last_id"], run["last_at"], run["count"], run["last_payload"], store_id, run["first_id"]),
                    )


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
        # v1 -> v2: ``active_session_state`` on snapshots (A78 §1). Existing
        # stores get the column via a guarded ALTER inside the write
        # transaction (concurrent openers race the check, not the ALTER);
        # fresh stores already have it from _DDL.
        with self._write() as tx:
            if not any(row[1] == "active_session_state" for row in tx.execute("PRAGMA table_info(snapshots)").fetchall()):
                try:
                    tx.execute("ALTER TABLE snapshots ADD COLUMN active_session_state TEXT")
                except sqlite3.OperationalError as exc:  # lost the race — column is there
                    if "duplicate column name" not in str(exc).lower():
                        raise
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
            active_session_state=snapshot.active_session_state,
        )
        snapshot_id = _snapshot_identity(snap)
        with self._write() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO snapshots(
                    snapshot_id, provider, principal_hash, bucket_id, observed_at,
                    telemetry_quality, used_percent, reset_at, limit_reached,
                    window_duration_seconds, raw_status, unavailable_reason,
                    active_session_state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    snap.active_session_state,
                    utc_iso(utc_now()),
                ),
            )
            return cur.rowcount > 0

    def record_refusal_snapshot(
        self,
        *,
        provider: str,
        reset_at: Optional[datetime],
        observed_at: Optional[datetime] = None,
    ) -> bool:
        """Record a provider refusal (429 / rate_limit_event) as an event-derived
        snapshot — the exact window boundary at the instant it was crossed, no
        polling and no cost (A78 §3).

        The refusal arrives without a quota-identity, so attribution reuses the
        principal(s) already known for ``provider`` that own a five_hour-style
        account bucket; an unattributable refusal records a
        ``quota.refusal_unattributed`` event instead of inventing identity.
        Idempotent per boundary: a repeat refusal naming the same reset instant
        does not write a second row. Returns True iff a snapshot was stored.
        """
        if reset_at is None:
            return False
        when = normalize_utc(observed_at) or utc_now()
        keys = [
            (str(row.get("provider") or ""), str(row.get("principal_hash") or ""), str(row.get("bucket_id") or ""))
            for row in self.latest_snapshots()
        ]
        targets = [k for k in keys if k[0] == provider and "five_hour" in k[2]]
        if not targets:
            self.add_event(
                "quota.refusal_unattributed",
                provider=provider,
                reason="no_known_five_hour_principal",
                payload={"reset_at": utc_iso(reset_at)},
            )
            return False
        stored = False
        for prov, principal_hash, bucket_id in targets:
            latest = self.latest_snapshot(prov, principal_hash, bucket_id)
            latest_reset = normalize_utc(latest.get("reset_at")) if latest else None
            if (
                latest is not None
                and latest.get("telemetry_quality") == TelemetryQuality.EVENT_DERIVED.value
                and latest_reset is not None
                and abs((latest_reset - reset_at).total_seconds()) <= _BOUNDARY_JITTER.total_seconds()
            ):
                continue  # same boundary already learned from a prior refusal
            stored = self.insert_snapshot(
                QuotaSnapshot(
                    provider=prov,
                    principal_hash=principal_hash,
                    bucket_id=bucket_id,
                    observed_at=when,
                    telemetry_quality=TelemetryQuality.EVENT_DERIVED,
                    used_percent=None,
                    reset_at=reset_at,
                    limit_reached=True,
                    raw_status="rate_limit_event",
                )
            ) or stored
        if stored:
            self.add_event(
                "quota.refusal_recorded",
                provider=provider,
                reason="rate_limit_event",
                payload={"reset_at": utc_iso(reset_at)},
            )
        return stored

    def _delete_batched(self, sql: str, params: tuple, batch_rows: int) -> int:
        """Run a ``... LIMIT ?`` DELETE repeatedly, one short write transaction
        per batch, until a batch comes back short (A93). Bounds how long any one
        statement holds the write lock; other writers interleave between batches."""
        batch = max(1, int(batch_rows))
        total = 0
        while True:
            with self._write() as conn:
                removed = conn.execute(sql, (*params, batch)).rowcount
            total += removed
            if removed < batch:
                return total

    @property
    def archive_path(self) -> Path:
        """The append-only history archive that sits next to this store (A93)."""
        return self._path.with_name(_ARCHIVE_FILENAME)

    def store_id(self) -> str:
        """This store file's identity (A93): a random id kept IN the store, so
        a rebuilt store gets a new one and archived event ids can never be
        mistaken for its own. Created lazily, only on the archive path."""
        with self._write() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute(
                "INSERT OR IGNORE INTO store_meta(key, value) VALUES ('store_id', ?)", (uuid.uuid4().hex,),
            )
            return str(conn.execute("SELECT value FROM store_meta WHERE key = 'store_id'").fetchone()[0])

    def _rotate_store_id(self, previous: str) -> str:
        """Give the store a fresh identity after an identity conflict (a store
        restored from an older copy keeps its old id). Compare-and-set, so
        concurrent pruners converge on one new id. Rows still present are then
        archived again under the new id: possibly counted twice, never lost."""
        with self._write() as conn:
            conn.execute(
                "UPDATE store_meta SET value = ? WHERE key = 'store_id' AND value = ?",
                (uuid.uuid4().hex, previous),
            )
            current = str(conn.execute("SELECT value FROM store_meta WHERE key = 'store_id'").fetchone()[0])
        logger.warning("event=quota_store_identity_rotated previous=%s current=%s", previous, current)
        return current

    def _archive_batch(self, write: Callable[["QuotaArchive"], None]) -> None:
        """Archive one prune batch BEFORE it is deleted. Any failure raises
        ``QuotaArchiveError`` so the caller deletes nothing."""
        try:
            write(QuotaArchive(self.archive_path))
        except QuotaStoreIdentityConflict:
            raise
        except Exception as exc:
            raise QuotaArchiveError(f"{type(exc).__name__}: {exc}") from exc

    def prune_snapshots(
        self,
        *,
        retention_days: int = _SNAPSHOT_RETENTION_DAYS,
        now: Optional[datetime] = None,
        batch_rows: int = _PRUNE_BATCH_ROWS,
        archive: bool = False,
    ) -> int:
        """Bounded history (A78 §4): delete redundant confirmation snapshots
        older than the cutoff while ALWAYS keeping (a) everything newer than
        the cutoff and (b) the FIRST sighting of each window boundary — that
        row is the anchored-window evidence; losing it would falsify
        classification. ``retention_days <= 0`` disables pruning entirely.
        Deletes in batches of ``batch_rows`` (A93). With ``archive`` each batch
        is first copied, full-fidelity, into ``snapshots_archive``; if that copy
        fails the batch is not deleted (``QuotaArchiveError``). Returns the
        number of rows removed."""
        if retention_days is None or int(retention_days) <= 0:
            return 0
        cutoff = utc_iso((normalize_utc(now) or utc_now()) - timedelta(days=int(retention_days)))
        if not archive:
            return self._delete_batched(
                f"DELETE FROM snapshots WHERE snapshot_id IN ({_PRUNABLE_SNAPSHOT_IDS_SQL})",
                (cutoff,),
                batch_rows,
            )
        batch = max(1, int(batch_rows))
        total = 0
        while True:
            rows = [
                dict(r) for r in self._conn().execute(
                    f"SELECT * FROM snapshots WHERE snapshot_id IN ({_PRUNABLE_SNAPSHOT_IDS_SQL})",
                    (cutoff, batch),
                ).fetchall()
            ]
            if not rows:
                return total
            self._archive_batch(lambda a: a.archive_snapshots(rows))
            with self._write() as conn:
                before = conn.total_changes
                conn.executemany(
                    "DELETE FROM snapshots WHERE snapshot_id = ?",
                    [(r["snapshot_id"],) for r in rows],
                )
                total += conn.total_changes - before
            if len(rows) < batch:
                return total

    def prune_events(
        self,
        *,
        retention_days: int = _EVENT_RETENTION_DAYS,
        now: Optional[datetime] = None,
        batch_rows: int = _PRUNE_BATCH_ROWS,
        archive: bool = False,
    ) -> int:
        """Bounded ``coordinator_events`` (A93): delete events older than the
        cutoff, oldest ``id`` first, in batches of ``batch_rows``. Rows stamped
        in the future (clock skew) simply survive until they age out.
        ``retention_days <= 0`` disables it. Returns the number of rows removed.

        With ``archive`` each batch is first compacted into ``event_runs`` and
        only then deleted (a failed archive raises ``QuotaArchiveError`` and
        deletes nothing). Archiving takes the contiguous ``id`` prefix older
        than the cutoff — it stops at the first row that is not yet old — so
        "consecutive identical events" means consecutive in the original table,
        and a run can be continued exactly across batches and passes."""
        if retention_days is None or int(retention_days) <= 0:
            return 0
        cutoff = utc_iso((normalize_utc(now) or utc_now()) - timedelta(days=int(retention_days)))
        if not archive:
            return self._delete_batched(
                """
                DELETE FROM coordinator_events WHERE id IN (
                  SELECT id FROM coordinator_events WHERE created_at < ? ORDER BY id LIMIT ?
                )
                """,
                (cutoff,),
                batch_rows,
            )
        # The pass works below a boundary fixed at its start: the first row that
        # is not yet old, capped at the newest row present, so an event that
        # arrives mid-pass is never taken.
        first_young = self._conn().execute(
            "SELECT id FROM coordinator_events WHERE created_at >= ? ORDER BY id LIMIT 1",
            (cutoff,),
        ).fetchone()
        newest = self._conn().execute("SELECT MAX(id) FROM coordinator_events").fetchone()[0]
        if newest is None:
            return 0
        boundary = min(int(first_young[0]) if first_young else int(newest) + 1, int(newest) + 1)
        store_id = self.store_id()
        batch = max(1, int(batch_rows))
        total = 0
        while True:
            rows = [
                dict(r) for r in self._conn().execute(
                    "SELECT * FROM coordinator_events WHERE id < ? ORDER BY id LIMIT ?",
                    (boundary, batch),
                ).fetchall()
            ]
            if not rows:
                return total
            try:
                self._archive_batch(lambda a: a.archive_events(rows, store_id=store_id))
            except QuotaStoreIdentityConflict:
                store_id = self._rotate_store_id(store_id)
                self._archive_batch(lambda a: a.archive_events(rows, store_id=store_id))
            with self._write() as conn:
                # The batch is the whole id-ordered prefix below the boundary, so
                # its id range holds exactly these rows (AUTOINCREMENT never
                # reuses a lower id).
                total += conn.execute(
                    "DELETE FROM coordinator_events WHERE id BETWEEN ? AND ?",
                    (rows[0]["id"], rows[-1]["id"]),
                ).rowcount
            if len(rows) < batch:
                return total

    def reclaim_space(self) -> str:
        """Return pages freed by pruning to the OS (A93). For a store only this
        process uses (the worker-local cache): a blocking ``VACUUM`` would stall
        the gateway/task-server that share the controller store.

        A store still at ``auto_vacuum=NONE`` is switched to INCREMENTAL, which
        needs one full ``VACUUM`` (sized by what survived the prune); after that
        each call is a cheap ``PRAGMA incremental_vacuum`` over the day's freed
        pages. The WAL is truncated afterwards so the rewrite does not linger in
        the ``-wal`` file. Returns ``"vacuum"`` or ``"incremental"``."""
        conn = self._conn()
        with self._write_lock:
            if conn.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
                conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
                conn.execute("VACUUM")
                mode = "vacuum"
            else:
                # The pragma frees one page per step; executescript (sqlite3_exec)
                # steps it to completion — execute().fetchall() freed ONE page.
                conn.executescript("PRAGMA incremental_vacuum;")
                mode = "incremental"
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        return mode

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

    def latest_snapshots(self) -> List[Dict[str, Any]]:
        """The newest snapshot per (provider, principal_hash, bucket) — the cheap
        read for "is the window spent right now?".

        ONE indexed ``LIMIT 1`` per known bucket (a handful), NOT a correlated
        subquery over the whole snapshot history. The previous form
        (``WHERE snapshot_id = (SELECT … WHERE i.provider = s.provider …)``) had
        no usable index for its correlation, so SQLite re-scanned the table for
        every row: at 53k rows that is ~2.8 BILLION row reads, measured at over
        two minutes on the gateway host. Any caller on a per-tick path (the
        Case quota-resume gate) must use this, not ``status()``.
        """
        conn = self._conn()
        # Keys come from the SNAPSHOTS themselves, not from `buckets`: an
        # observation can be recorded for a bucket that was never upserted
        # (adapter discovery failing after an observe, a replayed row), and
        # dropping it here would silently hide a spent window.
        keys = conn.execute(
            "SELECT DISTINCT provider, principal_hash, bucket_id FROM snapshots "
            "ORDER BY provider, bucket_id"
        ).fetchall()
        rows: List[Dict[str, Any]] = []
        for key in keys:
            row = self.latest_snapshot(
                str(key["provider"]), str(key["principal_hash"]), str(key["bucket_id"]),
            )
            if row is not None:
                rows.append(row)
        return rows

    def status(
        self,
        *,
        now: Optional[datetime] = None,
        next_observe_due_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        conn = self._conn()
        adapters = [dict(r) for r in conn.execute("SELECT * FROM adapter_status ORDER BY provider").fetchall()]
        buckets = [dict(r) for r in conn.execute("SELECT * FROM buckets ORDER BY provider, bucket_id").fetchall()]
        snapshots = self.latest_snapshots()
        window_states = _build_window_states(
            conn, buckets=buckets, snapshots=snapshots, now=now or utc_now(),
            next_observe_due_at=next_observe_due_at,
        )
        return {"adapters": adapters, "buckets": buckets, "latest_snapshots": snapshots, "window_states": window_states}


def _row_key(row: dict) -> tuple[str, str, str]:
    return (str(row.get("provider") or ""), str(row.get("principal_hash") or ""), str(row.get("bucket_id") or ""))


def _reset_history(conn: sqlite3.Connection, *, provider: str, principal_hash: str, bucket_id: str, reset_at: str | None) -> dict:
    """Boundary history for ONE bucket, bounded for the API (A78 §4).

    The store keeps everything; the read model must not ship all of it. This
    scans only the most recent ``_EVIDENCE_SCAN_MAX_ROWS`` rows (the bucket's
    index makes that a range scan), clusters boundaries whose instants sit
    within ``_BOUNDARY_JITTER`` of each other — the provider reports
    ``reset_at`` with sub-second drift, which used to split ONE window into
    dozens of evidence entries — and returns at most the
    ``_EVIDENCE_LIMIT`` most recent clusters. Counts are exact for what was
    scanned; when the scan hit its cap the older tail is summarised honestly
    in ``history_truncated`` instead of being silently dropped.
    """
    recent = conn.execute(
        """
        SELECT reset_at, observed_at, used_percent FROM (
            SELECT reset_at, observed_at, used_percent
            FROM snapshots
            WHERE provider = ? AND principal_hash = ? AND bucket_id = ?
              AND reset_at IS NOT NULL
            ORDER BY observed_at DESC, created_at DESC
            LIMIT ?
        ) ORDER BY observed_at ASC, used_percent ASC
        """,
        (provider, principal_hash, bucket_id, _EVIDENCE_SCAN_MAX_ROWS),
    ).fetchall()
    scanned_total = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE provider = ? AND principal_hash = ? AND bucket_id = ? AND reset_at IS NOT NULL",
        (provider, principal_hash, bucket_id),
    ).fetchone()[0]

    # Cluster ascending by boundary instant; a gap > jitter starts a new window.
    clusters: list[dict] = []
    for row in recent:
        instant = normalize_utc(row["reset_at"])
        if instant is None:
            continue
        if clusters and (instant - clusters[-1]["_max_instant"]) <= _BOUNDARY_JITTER:
            cluster = clusters[-1]
            cluster["_max_instant"] = max(cluster["_max_instant"], instant)
            cluster["last_seen_at"] = row["observed_at"]
            cluster["last_used_percent"] = row["used_percent"]
            continue
        clusters.append({
            "_max_instant": instant,
            "reset_at": utc_iso(instant),
            "first_seen_at": row["observed_at"],
            "last_seen_at": row["observed_at"],
            "first_used_percent": row["used_percent"],
            "last_used_percent": row["used_percent"],
        })

    current_since = None
    current_instant = normalize_utc(reset_at)
    if current_instant is not None:
        for cluster in clusters:
            canonical = normalize_utc(cluster["reset_at"])
            if canonical is not None and abs((canonical - current_instant).total_seconds()) <= _BOUNDARY_JITTER.total_seconds():
                current_since = cluster["first_seen_at"]
                break

    history = sorted(clusters, key=lambda c: (str(c["last_seen_at"]), str(c["reset_at"])), reverse=True)
    evidence_truncated = len(history) > _EVIDENCE_LIMIT or len(recent) >= _EVIDENCE_SCAN_MAX_ROWS
    return {
        "observed_reset_count": len(clusters),
        "current_reset_observed_since": current_since,
        "last_reset_change_at": history[0]["first_seen_at"] if len(history) > 1 else None,
        "reset_boundary_evidence": [
            {k: item[k] for k in ("reset_at", "first_seen_at", "last_seen_at", "first_used_percent", "last_used_percent")}
            for item in history[:_EVIDENCE_LIMIT]
        ],
        "history_rows_scanned": scanned_total,
        "evidence_truncated": evidence_truncated,
    }


def _window_state_for(
    bucket: Optional[dict],
    snapshot: dict,
    *,
    now: datetime,
    history: dict,
    next_observe_due_at: Optional[datetime] = None,
) -> dict:
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
    # Threaded from the adapter at observation time (A78 §1); absent on old
    # rows and error snapshots ⇒ "unknown", which blocks automation honestly.
    active_session_state = str(snapshot.get("active_session_state") or "unknown")
    blockers: list[str] = []

    age = (now - observed_at) if observed_at is not None else None
    limit_reached = bool(snapshot.get("limit_reached"))
    # Staleness that respects the observer's OWN contract (A78 §2): the
    # adaptive cadence deliberately sleeps until just before the next known
    # reset, so a reading stays current until its REPLACEMENT is due (+grace)
    # — not merely older than a fixed 15 minutes. And a SPENT reading remains
    # meaningful until its own reset instant passes: "the window is exhausted
    # until T" does not decay into "unknown" with age.
    stale_after = _SNAPSHOT_STALE_AFTER
    if next_observe_due_at is not None and observed_at is not None:
        # Freshness ends when the scheduled replacement is overdue, never at
        # "now": an overdue poll means stale, not an ever-extending lease.
        scheduled_end = next_observe_due_at + _STALE_SCHEDULE_GRACE
        stale_after = max(stale_after, scheduled_end - observed_at)
        observation_stale = now > scheduled_end and age > _SNAPSHOT_STALE_AFTER
    else:
        observation_stale = age is not None and age > stale_after
    spent_until_reset = (
        limit_reached
        and reset_at is not None
        and reset_at > now
    )
    if spent_until_reset:
        observation_stale = False

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
        # Provenance: an EVENT_DERIVED row is the provider's own refusal, not
        # an observer poll — consumers weighting evidence should know.
        "telemetry_source": "event_derived" if quality == TelemetryQuality.EVENT_DERIVED.value else "observed",
        "window_semantics": semantics,
        "classification_status": classification_status,
        "used_percent": used_percent,
        "limit_reached": bool(limit_reached),
        "observed_at": utc_iso(observed_at) or None,
        "observed_age_sec": int(age.total_seconds()) if age is not None else None,
        "stale_after_sec": int(stale_after.total_seconds()),
        "next_observe_due_at": utc_iso(next_observe_due_at) or None,
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


def _build_window_states(
    conn: sqlite3.Connection,
    *,
    buckets: list[dict],
    snapshots: list[dict],
    now: datetime,
    next_observe_due_at: Optional[datetime] = None,
) -> list[dict]:
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
        states.append(_window_state_for(
            bucket, snapshot, now=now, history=history, next_observe_due_at=next_observe_due_at,
        ))
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
        activation_model: str = "haiku",
    ) -> None:
        self.activation_model = activation_model
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

    async def activate(self, bucket_id: str = "five_hour") -> dict[str, Any]:
        """Start a window by spending the smallest turn the provider accepts
        (spec §8 ``QuotaAdapter.activate``). The scheduler asks the ADAPTER —
        provider contact never leaves this boundary.

        Invalidates the read cache afterwards so the very next ``observe`` sees
        the post-activation window rather than the pre-activation reading it
        would otherwise serve for ``cache_ttl_sec``.
        """
        from src.services.claude_usage_control import open_claude_window_with_minimal_turn

        result = await open_claude_window_with_minimal_turn(
            model=self.activation_model,
            cli_path=self.cli_path,
            timeout=self.timeout_sec,
        )
        self.model_invocations += 1
        self._cached_snapshot = None
        self._cached_snapshot_at = 0.0
        return result

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
        snapshot_retention_days: int = _SNAPSHOT_RETENTION_DAYS,
        event_retention_days: int = _EVENT_RETENTION_DAYS,
        reclaim_space: bool = False,
        archive_enabled: bool = True,
    ) -> None:
        self.store = store
        self.adapters = list(adapters)
        self.enabled = enabled
        self.observe_interval_sec = max(30, int(observe_interval_sec))
        self.observe_max_interval_sec = max(self.observe_interval_sec, int(observe_max_interval_sec))
        self.reset_probe_lead_sec = max(0, int(reset_probe_lead_sec))
        self.expected_schema_versions = expected_schema_versions or {}
        self.snapshot_retention_days = int(snapshot_retention_days)
        self.event_retention_days = int(event_retention_days)
        #: Only for a store this process owns alone (the worker-local cache).
        self.reclaim_space = bool(reclaim_space)
        #: Archive every pruned row to ``quota_archive.db`` before deleting it.
        self.archive_enabled = bool(archive_enabled)
        self._now = now
        self._event_handlers = list(event_handlers or [])
        self._task: Optional[asyncio.Task] = None
        self._last_now: Optional[datetime] = None
        self._last_prune_day: Optional[str] = None

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
        now = self._now()
        # The schedule the observer is actually keeping is part of honest
        # staleness (A78 §2): a reading is not "blind" while the next poll is
        # still in the future. Reuse ONE latest_snapshots fetch for both the
        # delay computation and the read model — status() on a full store is
        # the expensive call; paying it twice per request would be waste.
        try:
            delay = self.next_observe_delay_sec()
        except Exception:
            delay = self.observe_interval_sec
        data = self.store.status(
            now=now,
            next_observe_due_at=(normalize_utc(now) or utc_now()) + timedelta(seconds=delay),
        )
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
                await self.prune_once_daily()
                await asyncio.sleep(self.next_observe_delay_sec())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("quota_observe_loop_stopped err=%s", e)

    async def prune_once_daily(self) -> None:
        """Bounded history (A78 §4, A93): prune redundant old snapshots and old
        ``coordinator_events`` at most once per UTC day, best-effort — a failed
        prune must never kill the loop. The ONE retention seam: driven by
        ``_observe_loop`` on the controller and by the prewarmer's tick on the
        worker (which never starts this loop). Runs off the event loop thread:
        the DELETEs can scan tens of thousands of rows and must never block
        heartbeats/HTTP handling while they run.

        With ``archive_enabled`` every batch is archived to ``quota_archive.db``
        before it is deleted; if archiving fails nothing more is deleted, the
        failure is logged as ``quota_archive_failed`` and the day stays unmarked
        so the next tick retries."""
        try:
            today = utc_iso(self._now())[:10]
            if self._last_prune_day == today:
                return
            if self.snapshot_retention_days <= 0 and self.event_retention_days <= 0:
                return
            archive = self.archive_enabled
            now = self._now()
            pruned = await asyncio.to_thread(
                self.store.prune_snapshots, retention_days=self.snapshot_retention_days, now=now, archive=archive,
            )
            if pruned:
                logger.info(
                    "event=quota_snapshots_pruned rows=%d retention_days=%d archived=%s",
                    pruned, self.snapshot_retention_days, archive,
                )
            events = await asyncio.to_thread(
                self.store.prune_events, retention_days=self.event_retention_days, now=now, archive=archive,
            )
            if events:
                logger.info(
                    "event=quota_events_pruned rows=%d retention_days=%d archived=%s",
                    events, self.event_retention_days, archive,
                )
            self._last_prune_day = today
        except QuotaArchiveError as e:
            logger.warning("event=quota_archive_failed err=%s", e)
            return
        except Exception as e:
            logger.warning("event=quota_snapshot_prune_failed err=%s", e)
            return
        if self.reclaim_space and (pruned or events):
            try:
                mode = await asyncio.to_thread(self.store.reclaim_space)
                logger.info("event=quota_store_space_reclaimed mode=%s", mode)
            except Exception as e:
                logger.warning("event=quota_store_reclaim_failed err=%s", e)

    def next_observe_delay_sec(self, *, snapshots: Optional[List[Dict[str, Any]]] = None) -> int:
        now = normalize_utc(self._now()) or utc_now()
        if snapshots is None:
            snapshots = self.store.status().get("latest_snapshots", [])
        known_reset_delays: list[int] = []
        for row in snapshots:
            if row.get("limit_reached") == 1:
                # SPENT — and the provider says exactly when that ends. Sleep to
                # the boundary (plus a settle margin, because the reading right
                # ON it still shows the old window) instead of re-asking every
                # interval: on 2026-08-19 that cadence spent 16 consecutive
                # probes, each spawning a CLI subprocess, to re-learn a 17:20:00Z
                # instant already held since 15:39. Only an UNKNOWN or already
                # elapsed boundary justifies polling blind.
                spent_reset_at = normalize_utc(row.get("reset_at"))
                if spent_reset_at is None:
                    return self.observe_interval_sec
                seconds_until_reset = int((spent_reset_at - now).total_seconds())
                if seconds_until_reset <= 0:
                    return self.observe_interval_sec
                return min(
                    seconds_until_reset + _EXHAUSTED_SETTLE_SEC,
                    self.observe_max_interval_sec,
                )
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
            if snapshot.active_session_state is None:
                # Thread the adapter's live reading into the stored observation
                # (A78 §1) — event payloads alone made automation_ready
                # structurally unreachable.
                snapshot = replace(snapshot, active_session_state=active_label)
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
            active_session_state=previous.get("active_session_state"),
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
            activation_model=getattr(quota_cfg, "prewarm_model", "haiku"),
        ),
        UnsupportedQuotaAdapter("opencode", "opencode_is_provider_router_no_phase1_quota_owner"),
    ]


def build_quota_coordinator_from_config(
    *,
    enabled: Optional[bool] = None,
    event_handlers: Optional[Iterable[QuotaEventHandler]] = None,
    observe_locally: bool = True,
    reclaim_space: bool = False,
) -> QuotaWindowCoordinator:
    """Build the coordinator from config.

    ``observe_locally`` controls whether this process spawns the Claude harness
    to OBSERVE quota itself. On a controller-only host (the Docker controller,
    ``GATEWAY_LOCAL_EXECUTION_ENABLED=false``) there is no Claude binary or
    credentials, so it is False: the coordinator becomes ingest-only (no
    spawning adapters). Quota is then observed harness-side by a worker and POSTed
    to ``/telemetry/quota-observation``, which writes the same store this
    coordinator reads. A single-process/local-execution host keeps
    ``observe_locally=True`` for byte-identical legacy behaviour.

    ``reclaim_space`` returns pruned pages to the OS after the daily prune; pass
    it only for a store no other process opens (the worker-local cache, A93).
    """
    from config import config

    quota_cfg = getattr(config, "quota", None)
    db_path = getattr(quota_cfg, "db_path", "state/quota_windows.db")
    cfg_enabled = bool(getattr(quota_cfg, "enabled", False)) if enabled is None else bool(enabled)
    interval = int(getattr(quota_cfg, "observe_interval_sec", 300))
    max_interval = int(getattr(quota_cfg, "observe_max_interval_sec", 21600))
    lead = int(getattr(quota_cfg, "reset_probe_lead_sec", 900))
    retention = int(getattr(quota_cfg, "snapshot_retention_days", _SNAPSHOT_RETENTION_DAYS))
    event_retention = int(getattr(quota_cfg, "event_retention_days", _EVENT_RETENTION_DAYS))
    archive_enabled = bool(getattr(quota_cfg, "archive_enabled", True))
    adapters = build_default_quota_adapters() if observe_locally else []
    return QuotaWindowCoordinator(
        store=QuotaWindowStore(db_path),
        adapters=adapters,
        enabled=cfg_enabled,
        observe_interval_sec=interval,
        observe_max_interval_sec=max_interval,
        reset_probe_lead_sec=lead,
        event_handlers=event_handlers,
        snapshot_retention_days=retention,
        event_retention_days=event_retention,
        reclaim_space=reclaim_space,
        archive_enabled=archive_enabled,
    )


# ---------------------------------------------------------------------------
# Worker-originated ingest (controller/worker split)
# ---------------------------------------------------------------------------

_INGEST_STORE_LOCK = threading.Lock()
_INGEST_STORES: Dict[str, QuotaWindowStore] = {}


def _ingest_store(db_path: str) -> QuotaWindowStore:
    """A process-wide cached store per db_path (thread-safe: thread-local conns +
    write lock). Avoids re-running schema init on every ingest request."""
    with _INGEST_STORE_LOCK:
        store = _INGEST_STORES.get(db_path)
        if store is None:
            store = QuotaWindowStore(db_path)
            _INGEST_STORES[db_path] = store
        return store


async def ingest_worker_quota_observation(
    *,
    provider: str,
    node_id: str,
    principal_key: str = "",
    sdk_version: Optional[str] = None,
    claude_code_version: Optional[str] = None,
    observed_at: Optional[str] = None,
    usage: Optional[Dict[str, Any]] = None,
    error: str = "",
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a worker-originated quota observation through the existing store.

    Observation is done harness-side; this only validates and writes — NO Claude
    process is ever spawned here. On success it reuses
    ``QuotaWindowCoordinator.observe_once`` with a ``read_usage``-injected adapter,
    so the persisted rows match the pre-Docker in-process observer. On ``error``
    (or missing usage) it records the harness as adapter-unavailable WITHOUT
    overwriting the last good snapshots — so a transient harness failure surfaces
    as an explicit unavailable adapter, not as a flapped/empty window.
    """
    from config import config

    if db_path is None:
        db_path = getattr(getattr(config, "quota", None), "db_path", "state/quota_windows.db")
    store = _ingest_store(db_path)
    now = normalize_utc(observed_at) or utc_now()

    if provider != "claude":
        store.add_event(
            "quota.worker_observation_ignored",
            provider=provider,
            reason="unsupported_provider",
            payload={"node_id": node_id},
        )
        return {"accepted": False, "reason": "unsupported_provider", "provider": provider}

    if error or usage is None:
        reason = f"worker_harness_unavailable:{error or 'no_usage_payload'}"
        store.set_adapter_status(
            QuotaAdapterStatus(
                provider="claude",
                enabled=True,
                status="unavailable",
                reason=reason,
                adapter_version=ClaudeGetUsageQuotaAdapter.adapter_version,
                schema_version=ClaudeGetUsageQuotaAdapter.schema_version,
                last_checked_at=now,
            )
        )
        store.add_event(
            "quota.worker_observation_error",
            provider="claude",
            reason=reason,
            payload={"node_id": node_id},
        )
        try:
            from src.core.observability import emit_event

            emit_event("quota.worker_observation_error", provider="claude", node_id=node_id, reason=reason)
        except Exception:
            pass
        return {"accepted": False, "reason": "harness_error", "detail": reason}

    async def _read_usage() -> Dict[str, Any]:
        return usage

    adapter = ClaudeGetUsageQuotaAdapter(
        principal_key=principal_key or "",
        read_usage=_read_usage,
        claude_code_version_value=claude_code_version,
        now=lambda: now,
    )
    coordinator = QuotaWindowCoordinator(store=store, adapters=[adapter], enabled=True)
    await coordinator.observe_once()
    store.add_event(
        "quota.worker_observation_ingested",
        provider="claude",
        reason="",
        payload={"node_id": node_id, "sdk_version": sdk_version or ""},
    )
    try:
        from src.core.observability import emit_event

        emit_event("quota.worker_observation_ingested", provider="claude", node_id=node_id)
    except Exception:
        pass
    return {"accepted": True, "provider": "claude", "node_id": node_id}
