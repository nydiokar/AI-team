"""
Tiny in-process app + host metrics: request latency, event-loop lag, disk/CPU/memory pressure.

Why: "the app feels slow" could be the app, the device (this host is a Pi on a USB spinning
disk shared with other workloads) or the network. This answers it at near-zero cost:

  - request timing = a pure-ASGI middleware bumping in-memory histogram counters (no I/O),
    keyed by ROUTE TEMPLATE (bounded cardinality — crawlers cannot grow memory);
  - one 1 Hz asyncio tick measures event-loop lag; every 10 s it reads a few /proc files
    (memory-backed, microseconds); every 60 s it folds everything into ONE rollup;
  - the rollup goes to an in-memory ring (served by GET /api/metrics/system) and ONE small
    NDJSON append per minute (page-cache write, no fsync, size-rotated).
It deliberately never touches the DB — telemetry on the hot path is what hurt before.

Disable with APP_METRICS_ENABLED=false. Linux-only (/proc); any unreadable source degrades
to zeros instead of raising.
"""

import asyncio
import bisect
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Deque, Literal, Optional, Sequence

from pydantic import BaseModel
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

_BOUNDS_MS: tuple[float, ...] = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)
_ROW_HEAD: int = 4  # [count, sum_ms, max_ms, err5xx, *buckets]
_SLOW_FROM: int = _ROW_HEAD + _BOUNDS_MS.index(1000) + 1  # first bucket that means "> 1 s"
_MAX_ROUTE_KEYS: int = 200
_TOP_ROUTES: int = 15
_TICK_S: float = 1.0
_HOST_EVERY: int = 10
_ROLLUP_EVERY: int = 60
_RING_SIZE: int = 180
_LOG_MAX_BYTES: int = 5_000_000
_LOG_BACKUPS: int = 3
_PAGE_KB: int = os.sysconf("SC_PAGE_SIZE") // 1024 if hasattr(os, "sysconf") else 4
_CLK_TCK: int = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

RouteKey = tuple[str, str, str]

_lock: threading.Lock = threading.Lock()
_routes: dict[RouteKey, list[float]] = {}
_ring: Deque["Rollup"] = deque(maxlen=_RING_SIZE)
_task: Optional["asyncio.Task[None]"] = None


# --------------------------------------------------------------------------- request timing

def record_request(component: str, method: str, route: str, status: int, elapsed_ms: float) -> None:
    """Fold one finished request into the current window. O(1), lock held for microseconds."""
    key: RouteKey = (component, method, route)
    bucket: int = _ROW_HEAD + bisect.bisect_left(_BOUNDS_MS, elapsed_ms)
    with _lock:
        row = _routes.get(key)
        if row is None:
            if len(_routes) >= _MAX_ROUTE_KEYS:
                key = (component, "*", "overflow")
                row = _routes.get(key)
            if row is None:
                row = [0.0] * (_ROW_HEAD + len(_BOUNDS_MS) + 1)
                _routes[key] = row
        row[0] += 1
        row[1] += elapsed_ms
        row[2] = max(row[2], elapsed_ms)
        if status >= 500:
            row[3] += 1
        row[bucket] += 1


class RequestTimingMiddleware:
    """Pure-ASGI (not BaseHTTPMiddleware): cheaper, and safe for SSE/streaming responses.

    Measures time-to-response-start, i.e. server think time — what "slow" means to a client.
    """

    def __init__(self, app: ASGIApp, component: str) -> None:
        self.app = app
        self.component = component

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started: float = time.perf_counter()
        recorded: bool = False

        def _record(status: int) -> None:
            nonlocal recorded
            if recorded:
                return
            recorded = True
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            record_request(
                self.component, scope["method"], route, status,
                (time.perf_counter() - started) * 1000,
            )

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                _record(int(message["status"]))
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception:
            _record(500)
            raise


# --------------------------------------------------------------------------- host sampling

class _Raw(BaseModel):
    t: float
    cpu: list[int]
    blocked: int
    load1: float
    disk: list[int]
    swpin: int
    swpout: int
    majflt: int
    mem_avail_kb: int
    swap_used_kb: int
    temp_c: Optional[float]
    proc_jiffies: int
    proc_rss_kb: int
    proc_threads: int


class HostSample(BaseModel):
    cpu_busy_pct: float
    cpu_iowait_pct: float
    load1: float
    procs_blocked: float
    disk_util_pct: float
    disk_r_iops: float
    disk_w_iops: float
    disk_r_kbps: float
    disk_w_kbps: float
    disk_await_ms: float
    mem_avail_mb: float
    swap_used_mb: float
    swap_in_pages_s: float
    swap_out_pages_s: float
    major_faults_s: float
    temp_c: float
    proc_cpu_pct: float
    proc_rss_mb: float
    proc_threads: float


def _read(path: str) -> str:
    with open(path, "r") as f:
        return f.read()


def _kv_kb(text: str, key: str) -> int:
    for line in text.splitlines():
        if line.startswith(key + ":"):
            return int(line.split()[1])
    return 0


def _disk_counters(devno: Optional[tuple[int, int]]) -> list[int]:
    """[reads, sectors_r, ms_r, writes, sectors_w, ms_w, ms_io] for the probed device."""
    if devno is None:
        return []
    for line in _read("/proc/diskstats").splitlines():
        p = line.split()
        if len(p) >= 14 and (int(p[0]), int(p[1])) == devno:
            return [int(p[3]), int(p[5]), int(p[6]), int(p[7]), int(p[9]), int(p[10]), int(p[12])]
    return []


def _read_raw(devno: Optional[tuple[int, int]]) -> Optional[_Raw]:
    try:
        stat = _read("/proc/stat").splitlines()
        cpu = [int(x) for x in stat[0].split()[1:9]]
        blocked = next(int(l.split()[1]) for l in stat if l.startswith("procs_blocked"))
        vm = {l.split()[0]: int(l.split()[1]) for l in _read("/proc/vmstat").splitlines()
              if l.startswith(("pswpin", "pswpout", "pgmajfault"))}
        mem = _read("/proc/meminfo")
        self_stat = _read("/proc/self/stat").rsplit(")", 1)[1].split()
        try:
            temp: Optional[float] = int(_read("/sys/class/thermal/thermal_zone0/temp")) / 1000.0
        except (OSError, ValueError):
            temp = None
        return _Raw(
            t=time.monotonic(),
            cpu=cpu,
            blocked=blocked,
            load1=float(_read("/proc/loadavg").split()[0]),
            disk=_disk_counters(devno),
            swpin=vm.get("pswpin", 0),
            swpout=vm.get("pswpout", 0),
            majflt=vm.get("pgmajfault", 0),
            mem_avail_kb=_kv_kb(mem, "MemAvailable"),
            swap_used_kb=_kv_kb(mem, "SwapTotal") - _kv_kb(mem, "SwapFree"),
            temp_c=temp,
            proc_jiffies=int(self_stat[11]) + int(self_stat[12]),
            proc_rss_kb=int(_read("/proc/self/statm").split()[1]) * _PAGE_KB,
            proc_threads=len(os.listdir("/proc/self/task")),
        )
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _sample(prev: _Raw, cur: _Raw) -> HostSample:
    """Pure: turn two raw counter snapshots into rates/percentages."""
    dt: float = max(cur.t - prev.t, 1e-6)
    dc: list[int] = [c - p for c, p in zip(cur.cpu, prev.cpu)]
    total: int = max(sum(dc), 1)
    idle, iowait = dc[3], dc[4]
    dd: list[int] = (
        [c - p for c, p in zip(cur.disk, prev.disk)]
        if cur.disk and len(cur.disk) == len(prev.disk) else [0] * 7
    )
    reads, sec_r, ms_r, writes, sec_w, ms_w, ms_io = dd
    ops: int = reads + writes
    return HostSample(
        cpu_busy_pct=100.0 * (total - idle - iowait) / total,
        cpu_iowait_pct=100.0 * iowait / total,
        load1=cur.load1,
        procs_blocked=float(cur.blocked),
        disk_util_pct=min(100.0, 100.0 * ms_io / (dt * 1000.0)),
        disk_r_iops=reads / dt,
        disk_w_iops=writes / dt,
        disk_r_kbps=sec_r * 0.5 / dt,
        disk_w_kbps=sec_w * 0.5 / dt,
        disk_await_ms=(ms_r + ms_w) / ops if ops else 0.0,
        mem_avail_mb=cur.mem_avail_kb / 1024.0,
        swap_used_mb=cur.swap_used_kb / 1024.0,
        swap_in_pages_s=(cur.swpin - prev.swpin) / dt,
        swap_out_pages_s=(cur.swpout - prev.swpout) / dt,
        major_faults_s=(cur.majflt - prev.majflt) / dt,
        temp_c=cur.temp_c if cur.temp_c is not None else -1.0,
        proc_cpu_pct=100.0 * (cur.proc_jiffies - prev.proc_jiffies) / _CLK_TCK / dt,
        proc_rss_mb=cur.proc_rss_kb / 1024.0,
        proc_threads=float(cur.proc_threads),
    )


# --------------------------------------------------------------------------- rollup

class RouteRollup(BaseModel):
    component: str
    method: str
    route: str
    n: int
    p50_ms: float
    p95_ms: float
    max_ms: float
    slow: int
    err5xx: int


class Rollup(BaseModel):
    ts: str
    window_s: float
    lag_p95_ms: float
    lag_max_ms: float
    lag_over_100ms: int
    req_total: int
    req_slow: int
    req_5xx: int
    host_avg: dict[str, float]
    host_max: dict[str, float]
    routes: list[RouteRollup]


def _quantile(sorted_vals: list[float], q: float) -> float:
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))] if sorted_vals else 0.0


def _bucket_quantile(row: list[float], q: float) -> float:
    """Upper bound of the bucket holding quantile q, capped by the observed max."""
    target: float = q * row[0]
    seen: float = 0.0
    for i, n in enumerate(row[_ROW_HEAD:]):
        seen += n
        if seen >= target:
            return min(_BOUNDS_MS[i], row[2]) if i < len(_BOUNDS_MS) else row[2]
    return row[2]


def _rollup(window_s: float, lags_ms: list[float], samples: list[HostSample],
            routes: dict[RouteKey, list[float]]) -> Rollup:
    lags: list[float] = sorted(lags_ms)
    fields: list[str] = list(HostSample.model_fields)
    host_avg: dict[str, float] = {}
    host_max: dict[str, float] = {}
    for f in fields:
        vals = [getattr(s, f) for s in samples]
        if vals:
            host_avg[f] = round(sum(vals) / len(vals), 2)
            host_max[f] = round(max(vals), 2)
    per_route: list[RouteRollup] = [
        RouteRollup(
            component=k[0], method=k[1], route=k[2], n=int(r[0]),
            p50_ms=round(_bucket_quantile(r, 0.5), 1), p95_ms=round(_bucket_quantile(r, 0.95), 1),
            max_ms=round(r[2], 1), slow=int(sum(r[_SLOW_FROM:])), err5xx=int(r[3]),
        )
        for k, r in routes.items()
    ]
    # Keep the worst offenders by total time spent, so one line stays small.
    total_ms = {k: r[1] for k, r in routes.items()}
    per_route.sort(key=lambda x: total_ms[(x.component, x.method, x.route)], reverse=True)
    return Rollup(
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        window_s=round(window_s, 1),
        lag_p95_ms=round(_quantile(lags, 0.95), 1),
        lag_max_ms=round(lags[-1], 1) if lags else 0.0,
        lag_over_100ms=sum(1 for v in lags if v > 100),
        req_total=sum(p.n for p in per_route),
        req_slow=sum(p.slow for p in per_route),
        req_5xx=sum(p.err5xx for p in per_route),
        host_avg=host_avg,
        host_max=host_max,
        routes=per_route[:_TOP_ROUTES],
    )


def _drain_routes() -> dict[RouteKey, list[float]]:
    global _routes
    with _lock:
        drained, _routes = _routes, {}
    return drained


# --------------------------------------------------------------------------- health verdict

_HEALTH_WINDOW: int = 3  # rollups (~3 min): smooths blips so the banner doesn't flap


class HealthVerdict(BaseModel):
    status: Literal["ok", "warn", "bad"]
    cause: Literal["ok", "no_data", "disk", "memory", "thermal", "cpu", "event_loop", "slow_routes"]
    headline: str
    detail: str


def _level(value: float, warn: float, bad: float) -> int:
    return 2 if value >= bad else 1 if value >= warn else 0


def verdict(rollups: Sequence[Rollup]) -> HealthVerdict:
    """Answer "what is happening?" from the last few rollups — host first, then app.

    A host resource under pressure is reported as THE cause (the app is a victim of it);
    only when the host is healthy do we blame the gateway loop or a slow handler. Network
    is not measured: "slow endpoint, healthy host and loop" is the residual bucket.
    """
    win: list[Rollup] = list(rollups)[-_HEALTH_WINDOW:]
    if not win:
        return HealthVerdict(status="ok", cause="no_data", headline="", detail="")

    def avg(key: str) -> float:
        vals = [r.host_avg.get(key, 0.0) for r in win]
        return sum(vals) / len(vals)

    def peak(key: str) -> float:
        return max(r.host_max.get(key, 0.0) for r in win)

    slow: int = sum(r.req_slow for r in win)
    lag_p95: float = max(r.lag_p95_ms for r in win)
    victim: str = f"; {slow} slow requests (>1 s)" if slow else ""
    mem_free: float = min(r.host_avg.get("mem_avail_mb", 1e9) for r in win)

    host: list[tuple[int, str, str, str]] = [
        (max(_level(avg("disk_util_pct"), 60, 90), _level(avg("cpu_iowait_pct"), 25, 50)), "disk",
         "Host disk is saturated",
         f"disk {avg('disk_util_pct'):.0f}% busy, iowait {avg('cpu_iowait_pct'):.0f}%, "
         f"{avg('disk_await_ms'):.0f} ms/op{victim}"),
        (max(_level(-mem_free, -600, -300), _level(avg("swap_in_pages_s"), 100, 1000)), "memory",
         "Host is low on memory",
         f"{mem_free:.0f} MB available, swapping in {avg('swap_in_pages_s'):.0f} pages/s{victim}"),
        (_level(peak("temp_c"), 80, 85), "thermal", "Host is running hot (throttling likely)",
         f"SoC {peak('temp_c'):.0f} °C{victim}"),
        (_level(avg("cpu_busy_pct"), 75, 90), "cpu", "Host CPU is saturated",
         f"CPU {avg('cpu_busy_pct'):.0f}% busy{victim}"),
    ]
    worst = max(host, key=lambda h: h[0])  # max() keeps the first on ties → disk > memory > …
    if worst[0] > 0:
        return HealthVerdict(status="bad" if worst[0] == 2 else "warn", cause=worst[1],  # type: ignore[arg-type]
                             headline=worst[2], detail=worst[3])

    loop_level: int = _level(lag_p95, 250, 1000)
    slow_level: int = _level(slow, 5, 15)
    if loop_level > 0 and loop_level >= slow_level:
        return HealthVerdict(
            status="bad" if loop_level == 2 else "warn", cause="event_loop",
            headline="Gateway event loop is stalling",
            detail=f"loop lag p95 {lag_p95:.0f} ms while the host is healthy — app-side{victim}")
    if slow_level > 0:
        tally: dict[tuple[str, str, str], int] = {}
        p95: dict[tuple[str, str, str], float] = {}
        for r in win:
            for x in r.routes:
                k = (x.component, x.method, x.route)
                tally[k] = tally.get(k, 0) + x.slow
                p95[k] = max(p95.get(k, 0.0), x.p95_ms)
        top = max(tally, key=lambda k: tally[k]) if tally else None
        worst_route = f"; worst: {top[1]} {top[2]} (p95 {p95[top]:.0f} ms)" if top else ""
        return HealthVerdict(
            status="bad" if slow_level == 2 else "warn", cause="slow_routes",
            headline="Slow endpoints while host and loop are healthy",
            detail=f"{slow} requests >1 s in ~{len(win)} min{worst_route} — slow handler/DB or the network path")
    return HealthVerdict(status="ok", cause="ok", headline="", detail="")


def current_verdict() -> HealthVerdict:
    return verdict(list(_ring))


# --------------------------------------------------------------------------- sampler task

def _make_sink(path: Path) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    return RotatingFileHandler(path, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8")


def _write(sink: RotatingFileHandler, roll: Rollup) -> None:
    sink.emit(logging.makeLogRecord({"msg": roll.model_dump_json(), "args": None}))


async def _run(sink: Optional[RotatingFileHandler], devno: Optional[tuple[int, int]]) -> None:
    prev: Optional[_Raw] = _read_raw(devno)
    lags: list[float] = []
    samples: list[HostSample] = []
    ticks: int = 0
    last: float = time.monotonic()
    window_start: float = last
    while True:
        await asyncio.sleep(_TICK_S)
        try:
            now: float = time.monotonic()
            lags.append(max(0.0, (now - last - _TICK_S) * 1000.0))
            last = now
            ticks += 1
            if ticks % _HOST_EVERY == 0:
                cur = _read_raw(devno)
                if prev is not None and cur is not None:
                    samples.append(_sample(prev, cur))
                prev = cur
            if ticks % _ROLLUP_EVERY == 0:
                roll = _rollup(now - window_start, lags, samples, _drain_routes())
                lags, samples, window_start = [], [], now
                _ring.append(roll)
                if sink is not None:
                    await asyncio.to_thread(_write, sink, roll)
        except Exception:
            logger.debug("event=app_metrics_tick_failed", exc_info=True)


def start(logs_dir: str) -> None:
    """Start the sampler on the running loop. Idempotent; a no-op when disabled."""
    global _task
    if os.getenv("APP_METRICS_ENABLED", "true").lower() != "true":
        return
    if _task is not None and not _task.done():
        return
    devno: Optional[tuple[int, int]] = None
    sink: Optional[RotatingFileHandler] = None
    try:
        st = os.stat(logs_dir if os.path.isdir(logs_dir) else ".")
        devno = (os.major(st.st_dev), os.minor(st.st_dev))
        sink = _make_sink(Path(logs_dir) / "metrics.ndjson")
    except OSError:
        logger.warning("event=app_metrics_sink_unavailable logs_dir=%s", logs_dir)
    _task = asyncio.get_running_loop().create_task(_run(sink, devno), name="app-metrics")
    logger.info("event=app_metrics_started rollup_s=%d ring=%d", int(_TICK_S * _ROLLUP_EVERY), _RING_SIZE)


async def stop() -> None:
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def snapshot(minutes: int) -> dict[str, object]:
    """Last ``minutes`` rollups (oldest first) for the read endpoint."""
    rolls: list[Rollup] = list(_ring)[-max(1, minutes):]
    return {
        "enabled": _task is not None and not _task.done(),
        "rollup_seconds": int(_TICK_S * _ROLLUP_EVERY),
        "count": len(rolls),
        "rollups": [r.model_dump() for r in rolls],
    }
