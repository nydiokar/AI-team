"""Telemetry sink implementations for controller-local and split deployments."""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, List, Protocol

from src.control.telemetry_store import TelemetryStore
from src.core.telemetry import (
    TelemetryEvent,
    filter_events_for_detail_level,
    new_telemetry_id,
)

logger = logging.getLogger(__name__)


class TelemetrySink(Protocol):
    def emit(self, event: TelemetryEvent) -> None: ...
    def emit_many(self, events: Iterable[TelemetryEvent]) -> None: ...
    def flush(self) -> None: ...


class NullTelemetrySink:
    def emit(self, event: TelemetryEvent) -> None:
        return None

    def emit_many(self, events: Iterable[TelemetryEvent]) -> None:
        return None

    def flush(self) -> None:
        return None


class DatabaseTelemetrySink:
    """In-process task-server sink. Business logic should depend on the protocol."""

    def __init__(self, store: TelemetryStore) -> None:
        self.store = store

    def emit(self, event: TelemetryEvent) -> None:
        self.store.insert_events([event])

    def emit_many(self, events: Iterable[TelemetryEvent]) -> None:
        self.store.insert_events(list(events))

    def flush(self) -> None:
        return None


class BufferedHttpTelemetrySink:
    """Bounded HTTP batching with an atomic disk spool.

    Network failures never raise into task execution.  Calls to ``flush`` are
    synchronous by design; callers invoke it at invocation/result boundaries.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        node_id: str,
        spool_dir: str | Path,
        batch_size: int = 50,
        timeout: int = 10,
        flush_interval_ms: int = 1000,
        spool_max_bytes: int = 268_435_456,
        upload_max_bytes: int = 524_288,
        upload_max_attempts: int = 3,
        retry_backoff_seconds: float = 0.25,
        detailed_events: bool = True,
        spool_max_age_days: int = 7,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.node_id = node_id
        self.spool_dir = Path(spool_dir)
        self.batch_size = max(1, min(int(batch_size), 200))
        self.timeout = max(1, int(timeout))
        self.flush_interval_ms = max(100, int(flush_interval_ms))
        self.spool_max_bytes = max(1_048_576, int(spool_max_bytes))
        self.upload_max_bytes = max(65_536, int(upload_max_bytes))
        self.upload_max_attempts = max(1, int(upload_max_attempts))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.detailed_events = bool(detailed_events)
        self.spool_max_age_days = max(1, int(spool_max_age_days))
        self._events: List[TelemetryEvent] = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._last_failure_retryable = True

    def emit(self, event: TelemetryEvent) -> None:
        filtered = filter_events_for_detail_level(
            [event], detailed=self.detailed_events
        )
        if not filtered:
            return
        event = filtered[0]
        should_flush = False
        timer_to_start: threading.Timer | None = None
        with self._lock:
            self._events.append(event)
            should_flush = len(self._events) >= self.batch_size
            if not should_flush and self._timer is None:
                self._timer = threading.Timer(
                    self.flush_interval_ms / 1000.0, self.flush
                )
                self._timer.daemon = True
                timer_to_start = self._timer
        if timer_to_start is not None:
            timer_to_start.start()
        if should_flush:
            self.flush()

    def emit_many(self, events: Iterable[TelemetryEvent]) -> None:
        for event in events:
            self.emit(event)

    def flush(self) -> None:
        with self._lock:
            timer = self._timer
            self._timer = None
            if timer is not None:
                timer.cancel()
            if not self._events:
                return
            events = self._events
            self._events = []
        for batch_events in self._split_batches(events):
            body = self._batch_body(batch_events)
            if not self._post_batch(body):
                if self._last_failure_retryable:
                    self._spool(body)
                else:
                    logger.error(
                        "event=telemetry_batch_rejected node_id=%s batch_id=%s",
                        self.node_id,
                        body["batch_id"],
                    )

    def replay_spool(self) -> int:
        replayed = 0
        self._remove_expired_spool_files()
        try:
            paths = sorted(self.spool_dir.glob("*.json"))
        except Exception:
            return 0
        for path in paths:
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("event=telemetry_spool_invalid path=%s", path.name)
                continue
            if self._post_batch(body):
                try:
                    path.unlink()
                except OSError:
                    pass
                replayed += 1
            elif not self._last_failure_retryable:
                try:
                    path.unlink()
                except OSError:
                    pass
        return replayed

    def _remove_expired_spool_files(self) -> int:
        cutoff = time.time() - self.spool_max_age_days * 86400
        removed = 0
        try:
            for path in self.spool_dir.glob("*.json"):
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
        except Exception:
            logger.warning("event=telemetry_spool_expiry_failed", exc_info=True)
        if removed:
            logger.error(
                "event=telemetry_spool_expired node_id=%s dropped_batches=%d",
                self.node_id,
                removed,
            )
        return removed

    def _batch_body(self, events: List[TelemetryEvent]) -> dict:
        return {
            "batch_id": new_telemetry_id("batch"),
            "node_id": self.node_id,
            "events": [event.model_dump(mode="json") for event in events],
        }

    def _split_batches(
        self, events: List[TelemetryEvent]
    ) -> List[List[TelemetryEvent]]:
        """Bound batches by both event count and encoded request size."""
        batches: List[List[TelemetryEvent]] = []
        current: List[TelemetryEvent] = []
        for event in events:
            candidate = current + [event]
            body = self._batch_body(candidate)
            encoded_size = len(
                json.dumps(
                    body, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
            if current and (
                len(candidate) > self.batch_size
                or encoded_size > self.upload_max_bytes
            ):
                batches.append(current)
                current = [event]
            else:
                current = candidate
        if current:
            batches.append(current)
        return batches

    def _post_batch(self, body: dict) -> bool:
        self._last_failure_retryable = True
        if not self.base_url or not self.token:
            return False
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/telemetry/batches",
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        for attempt in range(1, self.upload_max_attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    response.read()
                return True
            except urllib.error.HTTPError as exc:
                if 400 <= exc.code < 500:
                    self._last_failure_retryable = False
                    logger.error(
                        "event=telemetry_upload_rejected node_id=%s status=%d",
                        self.node_id,
                        exc.code,
                    )
                    return False
                error_class = f"HTTP_{exc.code}"
            except Exception as exc:
                error_class = type(exc).__name__

            logger.warning(
                "event=telemetry_upload_failed node_id=%s error_class=%s attempt=%d",
                self.node_id,
                error_class,
                attempt,
            )
            if attempt < self.upload_max_attempts and self.retry_backoff_seconds:
                time.sleep(self.retry_backoff_seconds * (2 ** (attempt - 1)))
        return False

    def _spool(self, body: dict) -> None:
        try:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            batch_id = str(body.get("batch_id") or new_telemetry_id("batch"))
            final_path = self.spool_dir / f"{batch_id}.json"
            temp_path = self.spool_dir / f".{batch_id}.tmp"
            temp_path.write_text(
                json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temp_path.replace(final_path)
            self._enforce_spool_cap()
        except Exception as exc:
            logger.error(
                "event=telemetry_spool_failed node_id=%s error_class=%s",
                self.node_id,
                type(exc).__name__,
            )

    def _enforce_spool_cap(self) -> None:
        try:
            paths = sorted(
                self.spool_dir.glob("*.json"),
                key=lambda path: path.stat().st_mtime,
            )
            total = sum(path.stat().st_size for path in paths)
            dropped = 0
            for path in paths:
                if total <= self.spool_max_bytes:
                    break
                size = path.stat().st_size
                path.unlink()
                total -= size
                dropped += 1
            if dropped:
                logger.error(
                    "event=telemetry_spool_cap_enforced node_id=%s dropped_batches=%d",
                    self.node_id,
                    dropped,
                )
        except Exception:
            logger.warning("event=telemetry_spool_cap_check_failed", exc_info=True)


def build_runtime_telemetry_sink(
    *,
    node_id: str,
    base_url: str = "",
    token: str = "",
    logs_dir: str = "logs",
) -> TelemetrySink:
    """Build the safe runtime sink without importing gateway/worker classes."""
    try:
        from config import config
        if not config.telemetry.enabled:
            return NullTelemetrySink()
        resolved_url = (base_url or config.telemetry.task_server_url).rstrip("/")
        if not resolved_url:
            host = config.mesh.tailscale_ip or "127.0.0.1"
            resolved_url = f"http://{host}:{config.mesh.task_server_port}"
        resolved_token = token or config.mesh.worker_token
        if not resolved_token:
            return NullTelemetrySink()
        return BufferedHttpTelemetrySink(
            resolved_url,
            resolved_token,
            node_id=node_id,
            spool_dir=Path(logs_dir) / "telemetry_spool",
            batch_size=config.telemetry.upload_batch_size,
            flush_interval_ms=config.telemetry.upload_interval_ms,
            spool_max_bytes=config.telemetry.spool_max_bytes,
            upload_max_bytes=config.telemetry.upload_max_bytes,
            detailed_events=config.telemetry.detailed_events,
        )
    except Exception:
        return NullTelemetrySink()
