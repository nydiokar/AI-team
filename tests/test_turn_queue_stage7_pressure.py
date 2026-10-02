"""A82 Stage 7 — pressure rehearsal (packet §11, design §8). OPT-IN, OFFLINE.

Run explicitly (never part of a default run):

    A82_PRESSURE=1 .venv/bin/python -m pytest tests/test_turn_queue_stage7_pressure.py -s

Real pieces: the real control-API app (``build_control_api``) and task-server
app served by real uvicorn servers on 127.0.0.1 ephemeral ports (true HTTP
concurrency, chunked and stalled bodies over raw sockets), the real bound
orchestrator producer path (bare instance, as the Stage-4a suites), the real
admission service and a temp file-backed ``MeshDB``. Fake carriers drive the
real claim/start/result routes. No backend, CLI, model or network is touched
(producer-1 autouse spawn guard). Measured numbers are printed (``-s``) and, if
``A82_PRESSURE_OUT`` names a file, written there as JSON.

Bounds asserted (implemented values, verified in code): 4 concurrent queue
mutations (``turn_admission.MAX_CONCURRENT_ADMISSIONS``), per-session 20 /
fleet 50 waiting (``PER_SESSION_WAITING_CAP``, ``config.system.max_queue_size``),
100 MiB stored intent (``MAX_INTENT_BYTES_FLEET``), new route 256 KiB request /
16 KiB body text, compatibility route ``_INSTRUCTIONS_MAX_REQUEST_BYTES``
(≈3.8 MiB, the documented Stage-4a deviation from 2 MiB), 5 s mutation
lock/DB deadline (``ADMISSION_DEADLINE_SEC``) and 5 s body-read deadline
(``_BODY_READ_DEADLINE_SEC``), < 256 MiB incremental RSS for admission only.
"""
from __future__ import annotations

import asyncio
import json
import os
import resource
import sqlite3
import statistics
import threading
import time
from contextlib import suppress
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

import src.control.db as db_mod
from src.control import turn_admission as ta
from src.control import turn_queue as tq
from src.control import turn_scheduler as tsched
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus
from tests.test_turn_queue_4b import _wire
from tests.test_turn_queue_producer1 import (  # noqa: F401
    NOW, _flags, _no_cli_spawn, _register_carrier,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("A82_PRESSURE") != "1",
    reason="opt-in pressure rehearsal: set A82_PRESSURE=1",
)

TOKEN = "tok"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
TOL = 1.5  # bounded scheduling tolerance on a 4-core Pi (seconds)
METRICS: Dict[str, Any] = {}


def _record(key: str, value: Any) -> None:
    METRICS[key] = value
    print(f"[A82-PRESSURE] {key} = {json.dumps(value, default=str)}")
    out = os.environ.get("A82_PRESSURE_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(METRICS, fh, indent=2, default=str)


# --------------------------------------------------------------------------- #
# Harness: live servers, raw HTTP client, sampler
# --------------------------------------------------------------------------- #
class _LiveServer:
    """A real uvicorn server on its own thread + event loop (embedded shape)."""

    def __init__(self, app: Any) -> None:
        import uvicorn

        self.server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="warning", lifespan="off",
            loop="asyncio", http="h11", timeout_keep_alive=2,
        ))
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.port = 0
        self.thread = threading.Thread(target=self._run, name="a82-pressure-server", daemon=True)

    def _run(self) -> None:
        async def main() -> None:
            self.loop = asyncio.get_running_loop()
            await self.server.serve()

        asyncio.run(main())

    def __enter__(self) -> "_LiveServer":
        self.thread.start()
        end = time.monotonic() + 20
        while not self.server.started:
            assert time.monotonic() < end, "server did not start"
            time.sleep(0.02)
        self.port = int(self.server.servers[0].sockets[0].getsockname()[1])
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.server.should_exit = True
        self.thread.join(20)
        assert not self.thread.is_alive(), "server thread did not exit"

    def loop_probe(self) -> Tuple[float, int]:
        """(event-loop lag seconds, live asyncio tasks) — measured ON the loop."""
        assert self.loop is not None

        async def probe() -> int:
            return len(asyncio.all_tasks())

        t0 = time.monotonic()
        n = asyncio.run_coroutine_threadsafe(probe(), self.loop).result(10)
        return time.monotonic() - t0, n


class _Resp:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes,
                 elapsed: float, sent_at_response: int) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        self.elapsed = elapsed
        self.sent_at_response = sent_at_response

    def json(self) -> Any:
        return json.loads(self.body or b"null")

    @property
    def reason(self) -> str:
        try:
            detail = self.json().get("detail")
            return str(detail.get("reason") if isinstance(detail, dict) else detail)
        except Exception:  # noqa: BLE001
            return ""


async def _http(port: int, method: str, path: str, *, body: bytes = b"",
                headers: Optional[Dict[str, str]] = None, pieces: Optional[List[bytes]] = None,
                piece_delay: float = 0.0, chunked: bool = False, stall_after: Optional[int] = None,
                content_length: Optional[int] = None, timeout: float = 60.0) -> _Resp:
    """Raw HTTP/1.1 client: exact control over Content-Length, chunking, trickle
    and stall. Records how many body bytes had been SENT when the first
    response byte arrived (proves rejection before the whole body was read)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    hdrs: Dict[str, str] = {"Host": "127.0.0.1", "Connection": "close", **(headers or {})}
    if pieces is None:
        pieces = [body]
    if chunked:
        hdrs["Transfer-Encoding"] = "chunked"
    else:
        hdrs["Content-Length"] = str(content_length if content_length is not None
                                     else sum(len(p) for p in pieces))
    head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n"
    sent = 0

    async def send() -> None:
        nonlocal sent
        with suppress(ConnectionError, OSError):
            writer.write(head.encode())
            await writer.drain()
            for piece in pieces:
                if stall_after is not None and sent + len(piece) > stall_after:
                    piece = piece[: max(0, stall_after - sent)]
                if piece:
                    writer.write((f"{len(piece):x}\r\n".encode() + piece + b"\r\n") if chunked else piece)
                    await writer.drain()
                    sent += len(piece)
                if stall_after is not None and sent >= stall_after:
                    await asyncio.sleep(3600)
                if piece_delay:
                    await asyncio.sleep(piece_delay)
            if chunked:
                writer.write(b"0\r\n\r\n")
                await writer.drain()

    t0 = time.monotonic()
    sender = asyncio.create_task(send())
    try:
        first = await asyncio.wait_for(reader.read(1), timeout=timeout)
        sent_at = sent
        rest = await asyncio.wait_for(reader.read(), timeout=timeout)
    finally:
        sender.cancel()
        with suppress(BaseException):
            await sender
        writer.close()
        with suppress(BaseException):
            await writer.wait_closed()
    elapsed = time.monotonic() - t0
    raw = first + rest
    head_raw, _, payload = raw.partition(b"\r\n\r\n")
    lines = head_raw.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ", 2)[1]) if lines and lines[0] else 0
    headers_out = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    return _Resp(status, headers_out, payload, elapsed, sent_at)


def _rss_kib() -> int:
    with open("/proc/self/status", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return 0


class _Sampler:
    """Background sampler: RSS, threads, server-loop lag and task count."""

    def __init__(self, server: Optional[_LiveServer], period: float = 0.02) -> None:
        self.server = server
        self.period = period
        self.rss: List[int] = []
        self.threads: List[int] = []
        self.lag: List[float] = []
        self.tasks: List[int] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, name="a82-sampler", daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.rss.append(_rss_kib())
            self.threads.append(threading.active_count())
            if self.server is not None and self.server.loop is not None:
                with suppress(Exception):
                    lag, n = self.server.loop_probe()
                    self.lag.append(lag)
                    self.tasks.append(n)
            self._stop.wait(self.period)

    def __enter__(self) -> "_Sampler":
        self._t.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._stop.set()
        self._t.join(10)


def _pct(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


class _MutationProbe:
    """Wraps ``db.enqueue_turn`` (the queue mutation, called with the admission
    permit held): live/peak concurrency and per-call DB time. ``delay`` models a
    slow storage device while the permit is held."""

    def __init__(self, db: MeshDB, delay: float = 0.0) -> None:
        self.real: Callable[..., Any] = db.enqueue_turn
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.calls: List[float] = []
        db.enqueue_turn = self  # type: ignore[method-assign]

    def __call__(self, **kw: Any) -> Any:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        t0 = time.monotonic()
        try:
            if self.delay:
                time.sleep(self.delay)
            return self.real(**kw)
        finally:
            with self.lock:
                self.active -= 1
                self.calls.append(time.monotonic() - t0)


def _gateway(tmp_path: Any, monkeypatch: Any, sessions: int = 5) -> Tuple[MeshDB, Any]:
    """Temp DB + enrolled sessions + a registered managed carrier + the real
    bound orchestrator (bare instance) wired like the Stage-4 suites."""
    from src.core.session_task_queue import SessionTaskQueue
    from src.orchestrator import TaskOrchestrator
    from src.services.session_store import SessionStore

    db = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    for i in range(sessions):
        db.upsert_session(Session(
            session_id=f"sess-{i}", backend="claude", repo_path="/tmp/repo",
            status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW, machine_id="worker-a",
        ))
        db.enroll_session(f"sess-{i}")
    _register_carrier(db, "worker-a")
    o = TaskOrchestrator.__new__(TaskOrchestrator)
    o.session_store = SessionStore()
    o.task_queue = SessionTaskQueue(50, lambda _t: "")
    o.active_tasks = {}
    o._compact_injected_ids = set()
    o.events = []
    o._emit_event = lambda name, task, data=None: None
    o._emit_turn_telemetry = lambda name, task, data=None, **k: None
    _wire(o)
    return db, o


def _control_app(o: Any, monkeypatch: Any) -> Any:
    from src.control import control_api

    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    return control_api.build_control_api(o)


def _turn_body(text: str, op: str) -> bytes:
    return json.dumps({"body": text, "operation_id": op}).encode()


def _waiting(db: MeshDB) -> Dict[str, int]:
    rows = db._conn().execute(
        "SELECT session_id, COUNT(*) FROM mesh_tasks WHERE queue_protocol = 1 "
        "AND status IN ('queued', 'pending') GROUP BY session_id").fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def _executor_threads() -> int:
    return sum(1 for t in threading.enumerate() if t.name.startswith("asyncio_"))


# --------------------------------------------------------------------------- #
# PR01 — 100 simultaneous maximum-size requests
# --------------------------------------------------------------------------- #
def test_PR01_burst_100_max_size_requests(tmp_path: Any, monkeypatch: Any) -> None:
    db, o = _gateway(tmp_path, monkeypatch)
    probe = _MutationProbe(db, delay=0.05)
    # Worst-case max-size request: 16 KiB body text whose JSON escaping is
    # 6 bytes/char (\u0001) — ~96 KiB on the wire, under the 256 KiB cap.
    text = "\x01" * (16 * 1024)
    assert len(_turn_body(text, "op-x")) < 256 * 1024
    conns: List[int] = []
    real_connect = sqlite3.connect

    def _counting_connect(*a: Any, **k: Any) -> sqlite3.Connection:
        conns.append(1)
        return real_connect(*a, **k)

    monkeypatch.setattr(db_mod.sqlite3, "connect", _counting_connect)
    with _LiveServer(_control_app(o, monkeypatch)) as srv:
        rss0 = _rss_kib()
        maxrss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        threads0 = threading.active_count()

        async def burst() -> List[_Resp]:
            return await asyncio.gather(*[
                _http(srv.port, "POST", f"/api/sessions/sess-{i % 5}/turn-requests",
                      body=_turn_body(text, f"burst-{i}"), headers=H)
                for i in range(100)
            ])

        with _Sampler(srv) as sampler:
            t0 = time.monotonic()
            res = asyncio.run(burst())
            wall = time.monotonic() - t0
        statuses: Dict[int, int] = {}
        for r in res:
            statuses[r.status] = statuses.get(r.status, 0) + 1
        refused = [r for r in res if r.status == 429]
        assert set(statuses) <= {202, 429}, statuses
        assert all(r.reason == "capacity" and r.headers.get("retry-after") for r in refused)
        assert 2 <= probe.peak <= ta.MAX_CONCURRENT_ADMISSIONS, probe.peak
        rss_peak_delta = (max(sampler.rss) - rss0) / 1024.0
        assert rss_peak_delta < 256.0
        durations = [r.elapsed for r in res]
        burst_metrics = {
            "statuses": statuses, "wall_sec": round(wall, 3),
            "peak_concurrent_mutations": probe.peak,
            "request_sec_p50": round(_pct(durations, 0.5), 3),
            "request_sec_p95": round(_pct(durations, 0.95), 3),
            "request_sec_max": round(max(durations), 3),
            "rss_peak_delta_mib": round(rss_peak_delta, 1),
            "ru_maxrss_delta_mib": round((resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - maxrss0) / 1024.0, 1),
            "threads_before": threads0, "threads_peak": max(sampler.threads),
            "executor_threads_after": _executor_threads(),
            "sqlite_connections_opened": len(conns),
            "loop_lag_ms_p95": round(_pct(sampler.lag, 0.95) * 1000, 1),
            "loop_lag_ms_max": round(max(sampler.lag or [0.0]) * 1000, 1),
            "loop_tasks_peak": max(sampler.tasks or [0]),
        }
        assert burst_metrics["loop_lag_ms_max"] < 1000
        # Fill to the caps sequentially (no concurrency refusals): per-session
        # 20 first on sess-0, then the fleet 50.
        async def sequential() -> Tuple[List[_Resp], List[_Resp]]:
            sess0 = [await _http(srv.port, "POST", "/api/sessions/sess-0/turn-requests",
                                 body=_turn_body("s0", f"s0-{i}"), headers=H) for i in range(25)]
            fleet = [await _http(srv.port, "POST", f"/api/sessions/sess-{1 + i % 4}/turn-requests",
                                 body=_turn_body("f", f"fleet-{i}"), headers=H) for i in range(60)]
            return sess0, fleet

        sess0, fleet = asyncio.run(sequential())
    per = _waiting(db)
    assert per.get("sess-0") == tq.PER_SESSION_WAITING_CAP
    assert max(per.values()) <= tq.PER_SESSION_WAITING_CAP
    assert sum(per.values()) == 50
    assert sess0[-1].status == 429 and sess0[-1].reason == "capacity"
    assert fleet[-1].status == 429 and fleet[-1].reason == "capacity"
    totals = db.managed_waiting_totals()
    assert totals["bytes"] <= tq.MAX_INTENT_BYTES_FLEET
    assert ta._ADMISSION_PERMITS._value == ta.MAX_CONCURRENT_ADMISSIONS  # type: ignore[attr-defined]
    burst_metrics.update({"waiting_per_session": per, "waiting_total": sum(per.values()),
                          "stored_intent_bytes": totals["bytes"]})
    _record("PR01_burst", burst_metrics)


# --------------------------------------------------------------------------- #
# PR01b — the 100 MiB stored-intent bound binds on its own
# --------------------------------------------------------------------------- #
def test_PR01b_fleet_stored_intent_bound(tmp_path: Any, monkeypatch: Any) -> None:
    """Count caps raised for this call only (an operator can configure a larger
    ``max_queue_size``): the 100 MiB persisted-intent bound must still hold."""
    db = MeshDB(str(tmp_path / "mesh.db"))
    for i in range(4):
        db.upsert_session(Session(
            session_id=f"b-{i}", backend="claude", repo_path="/tmp/repo",
            status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW, machine_id="worker-a"))
        db.enroll_session(f"b-{i}")
    row_text = "y" * (1000 * 1024)  # stored twice (payload + prompt): ~2 MiB intent/row
    accepted = 0
    refused: Optional[tq.TurnQueueError] = None
    rss0 = _rss_kib()
    t0 = time.monotonic()
    for n in range(80):
        try:
            db.enqueue_turn(session_id=f"b-{n % 4}", body=row_text, operation_id=f"big-{n}",
                            turn_source="human", turn_kind="instruction", machine_id="worker-a",
                            require_enrolled=True, fleet_cap=1000)
            accepted += 1
        except tq.TurnQueueError as err:
            refused = err
            break
    totals = db.managed_waiting_totals()
    # Fleet budget exhausted is retryable capacity (429), not a per-row 413.
    assert refused is not None and refused.status_code == 429 and refused.code == "capacity"
    assert "byte budget" in str(refused.detail)
    assert totals["bytes"] <= tq.MAX_INTENT_BYTES_FLEET
    assert totals["bytes"] + 2 * len(row_text) > tq.MAX_INTENT_BYTES_FLEET
    _record("PR01b_intent_bytes", {
        "accepted_rows": accepted, "stored_intent_mib": round(totals["bytes"] / 2**20, 2),
        "refusal": refused.code, "sec": round(time.monotonic() - t0, 2),
        "rss_delta_mib": round((_rss_kib() - rss0) / 1024.0, 1),
    })


# --------------------------------------------------------------------------- #
# PR02 — body bounds: Content-Length, chunked oversize, slow bodies
# --------------------------------------------------------------------------- #
def test_PR02_body_bounds_before_whole_body_read(tmp_path: Any, monkeypatch: Any) -> None:
    from src.control import control_api

    db, o = _gateway(tmp_path, monkeypatch, sessions=1)
    new_cap = 256 * 1024
    compat_cap = control_api._INSTRUCTIONS_MAX_REQUEST_BYTES
    out: Dict[str, Any] = {"compat_cap_bytes": compat_cap}
    with _LiveServer(_control_app(o, monkeypatch)) as srv:
        async def run() -> None:
            path = "/api/sessions/sess-0/turn-requests"
            # (a) declared oversize: refused on the header, body never sent.
            r = await _http(srv.port, "POST", path, headers=H, pieces=[b""],
                            content_length=new_cap + 1, timeout=10)
            assert r.status == 413 and r.sent_at_response == 0
            out["new_declared_oversize"] = {"status": r.status, "sec": round(r.elapsed, 3)}
            # (b) chunked oversize: 8 KiB chunks, total 1 MiB; refusal must
            # arrive while the client is still sending (not a whole-body read).
            chunks = [b"a" * 8192] * 128
            r = await _http(srv.port, "POST", path, headers=H, pieces=chunks, chunked=True,
                            piece_delay=0.005, timeout=20)
            assert r.status == 413
            assert r.sent_at_response <= new_cap + 64 * 1024, r.sent_at_response
            out["new_chunked_oversize"] = {"status": r.status, "sent_kib_at_response":
                                           round(r.sent_at_response / 1024, 1)}
            # (c) body text 16 KiB boundary (UTF-8 bytes of the decoded value).
            ok = await _http(srv.port, "POST", path, headers=H,
                             body=_turn_body("é" * 8192, "edge-ok"))
            big = await _http(srv.port, "POST", path, headers=H,
                              body=_turn_body("é" * 8192 + "x", "edge-big"))
            assert ok.status == 202 and big.status == 422
            out["body_text_16kib"] = {"at_cap": ok.status, "over_cap": big.status}
            # (d) compatibility route: declared + chunked oversize.
            r = await _http(srv.port, "POST", "/api/instructions", headers=H, pieces=[b""],
                            content_length=compat_cap + 1, timeout=10)
            assert r.status == 413 and r.sent_at_response == 0
            big_chunks = [b"b" * 65536] * ((compat_cap // 65536) + 16)
            r = await _http(srv.port, "POST", "/api/instructions", headers=H, pieces=big_chunks,
                            chunked=True, piece_delay=0.002, timeout=30)
            assert r.status == 413 and r.sent_at_response <= compat_cap + 256 * 1024
            out["compat_chunked_oversize"] = {"status": r.status, "sent_mib_at_response":
                                              round(r.sent_at_response / 2**20, 2)}
            # (e) stalled body: 10 of 1000 declared bytes, then silence → 408
            # at the 5 s body-read deadline (not held open).
            r = await _http(srv.port, "POST", path, headers=H, body=b"x" * 1000,
                            stall_after=10, timeout=20)
            assert r.status == 408 and r.reason == "body_read_timeout"
            assert control_api._BODY_READ_DEADLINE_SEC - 0.2 <= r.elapsed <= control_api._BODY_READ_DEADLINE_SEC + TOL
            out["stalled_body"] = {"status": r.status, "sec": round(r.elapsed, 3)}
            # (f) slow but complete body inside the deadline → accepted.
            body = _turn_body("slow", "slow-ok")
            pieces = [body[i:i + 8] for i in range(0, len(body), 8)]
            r = await _http(srv.port, "POST", path, headers=H, pieces=pieces,
                            piece_delay=3.0 / len(pieces), timeout=20)
            assert r.status == 202
            out["trickled_3s_body"] = {"status": r.status, "sec": round(r.elapsed, 3)}
            # (g) the deadline is TOTAL, not idle: a trickle that never pauses
            # long but outlasts 5 s is cut at ~5 s.
            long_body = _turn_body("z" * 4000, "too-slow")
            pieces = [long_body[i:i + 64] for i in range(0, len(long_body), 64)]
            r = await _http(srv.port, "POST", path, headers=H, pieces=pieces,
                            piece_delay=8.0 / len(pieces), timeout=20)
            assert r.status == 408 and r.elapsed <= control_api._BODY_READ_DEADLINE_SEC + TOL
            out["trickle_past_deadline"] = {"status": r.status, "sec": round(r.elapsed, 3)}

        asyncio.run(run())
    assert set(_waiting(db)) <= {"sess-0"} and sum(_waiting(db).values()) == 2  # ok + slow-ok
    _record("PR02_body_bounds", out)


# --------------------------------------------------------------------------- #
# PR03 — held SQLite write lock: deadlines, no false acceptance, liveness,
# permits held until the timed-out threads exit, no orphan threads, progress.
# --------------------------------------------------------------------------- #
def test_PR03_held_write_lock(tmp_path: Any, monkeypatch: Any) -> None:
    db, o = _gateway(tmp_path, monkeypatch)
    probe = _MutationProbe(db)
    out: Dict[str, Any] = {}
    with _LiveServer(_control_app(o, monkeypatch)) as srv:
        holder = sqlite3.connect(str(tmp_path / "mesh.db"), isolation_level=None, timeout=0)

        def one_round(tag: str) -> Dict[str, Any]:
            holder.execute("BEGIN IMMEDIATE")  # the external writer holds the lock
            permits_mid: List[int] = []
            reads: List[float] = []

            async def run() -> List[_Resp]:
                reqs = [asyncio.create_task(_http(
                    srv.port, "POST", f"/api/sessions/sess-{i % 5}/turn-requests",
                    body=_turn_body("held", f"{tag}-{i}"), headers=H, timeout=30))
                    for i in range(10)]
                await asyncio.sleep(2.0)
                permits_mid.append(ta._ADMISSION_PERMITS._value)  # type: ignore[attr-defined]
                for _ in range(5):  # liveness under the held lock
                    r1 = await _http(srv.port, "GET", "/health", headers=H, timeout=5)
                    r2 = await _http(srv.port, "GET", "/api/sessions/sess-0/turn-requests",
                                     headers=H, timeout=5)
                    assert r1.status == 200 and r2.status == 200
                    reads.extend([r1.elapsed, r2.elapsed])
                return list(await asyncio.gather(*reqs))

            with _Sampler(srv) as sampler:
                res = asyncio.run(run())
            holder.execute("ROLLBACK")
            failed = [r for r in res if r.status == 503]
            fast = [r for r in res if r.status == 429]
            assert len(failed) + len(fast) == len(res), [r.status for r in res]
            assert all(r.reason == "backing_store" for r in failed)
            assert len(failed) <= ta.MAX_CONCURRENT_ADMISSIONS and failed
            assert all(tq.ADMISSION_DEADLINE_SEC - 0.3 <= r.elapsed <= tq.ADMISSION_DEADLINE_SEC + TOL
                       for r in failed), [round(r.elapsed, 2) for r in failed]
            assert all(r.elapsed < 1.5 for r in fast)
            assert permits_mid == [0]  # held by the blocked threads, not released early
            assert max(reads) < 1.0 and max(sampler.lag or [0.0]) < 0.5
            return {
                "statuses": {"503": len(failed), "429": len(fast)},
                "deadline_sec": [round(r.elapsed, 3) for r in failed],
                "permits_free_mid_hold": permits_mid[0],
                "read_sec_max": round(max(reads), 3),
                "loop_lag_ms_max": round(max(sampler.lag or [0.0]) * 1000, 1),
                "db_call_sec_max": round(max(probe.calls), 3),
            }

        out["round1"] = one_round("r1")
        threads_after_1 = threading.active_count()
        out["round2"] = one_round("r2")
        threads_after_2 = threading.active_count()
        assert threads_after_2 <= threads_after_1  # no orphan thread accumulation
        assert ta._ADMISSION_PERMITS._value == ta.MAX_CONCURRENT_ADMISSIONS  # type: ignore[attr-defined]
        assert sum(_waiting(db).values()) == 0  # no false acceptance
        out["threads_after_rounds"] = [threads_after_1, threads_after_2]
        out["executor_threads"] = _executor_threads()

        # Body-read deadline and mutation deadline are separate budgets: a
        # 3 s upload followed by a held lock spends ~3 s + ~5 s.
        holder.execute("BEGIN IMMEDIATE")

        async def both() -> _Resp:
            body = _turn_body("both", "both-1")
            pieces = [body[i:i + 6] for i in range(0, len(body), 6)]
            return await _http(srv.port, "POST", "/api/sessions/sess-1/turn-requests",
                               headers=H, pieces=pieces, piece_delay=3.0 / len(pieces), timeout=30)

        n_calls = len(probe.calls)
        r = asyncio.run(both())
        holder.execute("ROLLBACK")
        db_sec = probe.calls[n_calls]
        assert r.status == 503 and r.reason == "backing_store"
        assert db_sec <= tq.ADMISSION_DEADLINE_SEC + TOL
        assert 3.0 + tq.ADMISSION_DEADLINE_SEC - 0.5 <= r.elapsed <= 3.0 + tq.ADMISSION_DEADLINE_SEC + TOL
        out["body_plus_db"] = {"total_sec": round(r.elapsed, 3), "db_sec": round(db_sec, 3)}

        # Lock released: admissions commit and the scheduler activates heads.
        async def after() -> List[_Resp]:
            return [await _http(srv.port, "POST", f"/api/sessions/sess-{i}/turn-requests",
                                body=_turn_body("after", f"after-{i}"), headers=H) for i in range(3)]

        assert [x.status for x in asyncio.run(after())] == [202, 202, 202]
        holder.close()
    res = asyncio.run(tsched.run_scheduler_pass(
        db, o._prepare_managed_turn, allowance=ta.SharedWaitingAllowance()))
    assert res.activated == 3
    out["after_release_activated"] = res.activated
    _record("PR03_held_lock", out)


# --------------------------------------------------------------------------- #
# PR04 — 1000 vs 100000 completed rows, same 50 waiting: plans and cost
# --------------------------------------------------------------------------- #
def _seed_history(db: MeshDB, n_completed: int, waiting_sessions: int, per_session: int) -> None:
    for i in range(waiting_sessions):
        db.upsert_session(Session(
            session_id=f"h-{i}", backend="claude", repo_path="/tmp/repo",
            status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW, machine_id="worker-a"))
        db.enroll_session(f"h-{i}")
    _register_carrier(db, "worker-a")
    db.enqueue_turn(task_id="tmpl", session_id="h-0", backend="claude", action="resume_session",
                    payload={"task_id": "tmpl", "prompt": "p"}, turn_source="human",
                    turn_kind="instruction", machine_id="worker-a", operation_id="tmpl")
    conn = db._conn()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(mesh_tasks)").fetchall()]
    tmpl = dict(conn.execute("SELECT * FROM mesh_tasks WHERE id = 'tmpl'").fetchone())
    conn.execute("DELETE FROM mesh_tasks WHERE id = 'tmpl'")
    rows = []
    for k in range(n_completed):
        r = dict(tmpl)
        r.update(id=f"done-{k}", session_id=f"h-{k % waiting_sessions}", status="completed",
                 queue_sequence=10_000_000 + k, idempotency_key=f"done-{k}",
                 coalesce_key=None, completed_at=NOW)
        rows.append(tuple(r[c] for c in cols))
    conn.execute("BEGIN")
    conn.executemany(f"INSERT INTO mesh_tasks ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", rows)
    conn.execute("COMMIT")
    conn.execute("ANALYZE")
    for i in range(waiting_sessions * per_session):
        db.enqueue_turn(session_id=f"h-{i % waiting_sessions}", body=f"w{i}", operation_id=f"w-{i}",
                        turn_source="human", turn_kind="instruction", machine_id="worker-a")


def _traced(db: MeshDB, fn: Callable[[], Any]) -> List[str]:
    stmts: List[str] = []
    conn = db._conn()
    conn.set_trace_callback(stmts.append)
    try:
        fn()
    finally:
        conn.set_trace_callback(None)
    return stmts


def _plan_problems(db: MeshDB, stmts: List[str]) -> List[str]:
    problems: List[str] = []
    conn = db._conn()
    for sql in stmts:
        head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if head not in ("SELECT", "UPDATE", "DELETE", "INSERT", "WITH") or "mesh_tasks" not in sql:
            continue
        try:
            plan = [str(r[3]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()]
        except sqlite3.Error:
            continue
        # A temp b-tree is acceptable only over the BOUNDED waiting/open subset
        # (partial indexes; ≤ fleet cap rows), never over history.
        bounded = any(ix in " ".join(plan) for ix in (
            "idx_mesh_turns_waiting", "idx_mesh_turns_session_open"))
        managed = "queue_protocol = 1" in sql
        import re as _re

        aliases = {"mesh_tasks"} | {
            a for a in _re.findall(r"mesh_tasks\s+(?:AS\s+)?(\w+)", sql)
            if a.upper() not in ("INDEXED", "WHERE", "JOIN", "LEFT", "ON", "SET", "GROUP",
                                 "ORDER", "LIMIT", "VALUES", "INNER", "CROSS")
        }
        for line in plan:
            parts = line.split()
            on_tasks = len(parts) > 1 and parts[0] in ("SCAN", "SEARCH") and parts[1] in aliases
            full_scan = on_tasks and parts[0] == "SCAN" and " USING " not in line
            # A managed statement reaching mesh_tasks through a protocol-agnostic
            # session/time index walks that session's whole HISTORY.
            history_walk = managed and any(ix in line for ix in (
                "idx_mesh_tasks_session ", "idx_mesh_tasks_session_created", "idx_mesh_tasks_created"))
            if full_scan or (on_tasks and history_walk):
                problems.append(f"{line} :: {sql[:160]}")
            elif "TEMP B-TREE" in line:
                if bounded:
                    BOUNDED_TEMP.append(f"{line} :: {' | '.join(plan)}")
                else:
                    problems.append(f"{line} :: {sql[:160]}")
    return problems


BOUNDED_TEMP: List[str] = []


@pytest.mark.parametrize("n_completed", [1000, 100_000])
def test_PR04_history_does_not_change_plans_or_cost(tmp_path: Any, monkeypatch: Any, n_completed: int) -> None:
    db = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    t0 = time.monotonic()
    _seed_history(db, n_completed, waiting_sessions=10, per_session=5)
    seed_sec = time.monotonic() - t0

    def lifecycle() -> None:
        db.select_eligible_turn_heads(25)
        db.managed_waiting_totals()
        db.count_slot_waiting_sessions()
        db.next_turn_wake_at()
        db.list_lineage_recovery(25)
        db.list_void_lineage(25)
        db.requeue_turns_on_dead_carriers(25)
        db.session_turn_queue_states([f"h-{i}" for i in range(10)])
        db.list_turn_requests("h-0", after_sequence=0, limit=50)
        db.enqueue_turn(session_id="h-1", body="probe", operation_id=f"probe-{n_completed}", fleet_cap=1000,
                        turn_source="human", turn_kind="instruction", machine_id="worker-a")
        with suppress(tq.TurnQueueError):
            db.unenroll_session_drained("h-9")  # refused (waiting rows): plan still traced

    stmts = _traced(db, lifecycle)
    problems = _plan_problems(db, stmts)
    assert not problems, problems
    timings: List[float] = []
    for _ in range(5):
        t1 = time.monotonic()
        db.select_eligible_turn_heads(25)
        db.managed_waiting_totals()
        db.count_slot_waiting_sessions()
        db.next_turn_wake_at()
        timings.append(time.monotonic() - t1)
    heads_10 = len(_traced(db, lambda: db.select_eligible_turn_heads(25)))
    METRICS.setdefault("PR04_history", {})[str(n_completed)] = {
        "seed_sec": round(seed_sec, 2), "statements_traced": len(stmts),
        "heads_query_statements": heads_10,
        "bounded_temp_btrees": sorted(set(BOUNDED_TEMP)),
        "scheduler_pass_reads_ms_median": round(statistics.median(timings) * 1000, 2),
    }
    _record("PR04_history", METRICS["PR04_history"])
    if len(METRICS["PR04_history"]) == 2:
        small, large = (METRICS["PR04_history"][k] for k in ("1000", "100000"))
        assert small["heads_query_statements"] == large["heads_query_statements"]
        assert large["scheduler_pass_reads_ms_median"] <= max(
            3 * small["scheduler_pass_reads_ms_median"], 20.0)


def test_PR04b_case_eligibility_queries_stay_batched(tmp_path: Any, monkeypatch: Any) -> None:
    """Head selection issues the same number of statements for 10 and 50
    waiting heads (eligibility incl. the Case binding gate is one query, not
    one per head)."""
    counts: Dict[int, int] = {}
    for waiting in (10, 50):
        db = MeshDB(str(tmp_path / f"m{waiting}.db"))
        _seed_history(db, 10, waiting_sessions=waiting, per_session=1)
        counts[waiting] = len(_traced(db, lambda: db.select_eligible_turn_heads(25)))
    assert counts[10] == counts[50]
    _record("PR04b_heads_statements", counts)


# --------------------------------------------------------------------------- #
# PR05 — admission pressure does not starve result / heartbeat capacity
# --------------------------------------------------------------------------- #
def test_PR05_lifecycle_not_starved_by_admission_pressure(tmp_path: Any, monkeypatch: Any) -> None:
    import src.control.node_registry as nr_mod
    import src.control.task_server as ts_mod

    db, o = _gateway(tmp_path, monkeypatch)
    # One turn ready for the carrier before the storm.
    t1 = str(db.enqueue_turn(session_id="sess-4", body="run me", operation_id="ready",
                             turn_source="human", turn_kind="instruction", machine_id="worker-a"))
    db.activate_turn(t1)
    probe = _MutationProbe(db, delay=1.0)  # every queue mutation holds its permit ~1 s
    monkeypatch.setattr(ts_mod, "get_db", lambda: db)
    monkeypatch.setattr(nr_mod, "_registry", nr_mod.NodeRegistry())
    monkeypatch.setattr(ts_mod, "_worker_token", lambda: TOKEN)
    out: Dict[str, Any] = {}
    with _LiveServer(_control_app(o, monkeypatch)) as gw, _LiveServer(ts_mod.app) as carrier_api:
        async def run() -> None:
            reg = await _http(carrier_api.port, "POST", "/nodes/register", headers=H, body=json.dumps({
                "node_id": "worker-a", "tailscale_ip": "127.0.0.1", "api_port": 0,
                "incarnation_id": "inc-1", "capabilities": {
                    "backends": ["claude"], "queue_protocols": [0, 1], "managed_backends": ["claude"]},
            }).encode())
            assert reg.status == 200, reg.body
            storm = [asyncio.create_task(_http(
                gw.port, "POST", f"/api/sessions/sess-{i % 4}/turn-requests",
                body=_turn_body("storm", f"storm-{i}"), headers=H, timeout=60)) for i in range(60)]
            await asyncio.sleep(0.3)
            assert probe.active == ta.MAX_CONCURRENT_ADMISSIONS  # saturated
            lat: Dict[str, float] = {}
            r = await _http(carrier_api.port, "POST", "/nodes/heartbeat", headers=H, body=json.dumps(
                {"node_id": "worker-a"}).encode())
            lat["heartbeat"] = r.elapsed
            assert r.status == 200, r.body
            r = await _http(carrier_api.port, "POST", f"/tasks/{t1}/claim-managed", headers=H,
                            body=json.dumps({"node_id": "worker-a", "incarnation_id": "inc-1"}).encode())
            lat["claim"] = r.elapsed
            assert r.status == 200, r.body
            tok = r.json()["claim_token"]
            r = await _http(carrier_api.port, "POST", f"/tasks/{t1}/start-managed", headers=H,
                            body=json.dumps({"node_id": "worker-a", "claim_token": tok,
                                             "incarnation_id": "inc-1"}).encode())
            lat["start"] = r.elapsed
            assert r.status == 200, r.body
            r = await _http(carrier_api.port, "POST", f"/tasks/{t1}/result-managed", headers=H,
                            body=json.dumps({"node_id": "worker-a", "claim_token": tok,
                                             "success": True,
                                             "output": "done", "errors": [], "files_modified": [],
                                             "execution_time": 0.1, "return_code": 0}).encode())
            lat["result"] = r.elapsed
            assert r.status == 200, r.body
            r = await _http(gw.port, "GET", "/api/sessions/sess-4/turn-requests", headers=H)
            lat["gateway_read"] = r.elapsed
            assert r.status == 200
            still_saturated = probe.active
            results = await asyncio.gather(*storm)
            out.update({k: round(v, 3) for k, v in lat.items()})
            out["mutations_active_during_lifecycle"] = still_saturated
            out["storm_statuses"] = sorted({x.status for x in results})
            assert max(lat.values()) < 1.0, lat

        asyncio.run(run())
    assert db.get_task(t1)["status"] == "completed"
    _record("PR05_lifecycle_under_pressure", out)
