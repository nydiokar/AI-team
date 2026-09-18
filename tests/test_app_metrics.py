"""app_metrics — request timing, host sampling and the rollup/ring/NDJSON pipeline (no network)."""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.control import app_metrics as am
from src.control import control_api


@pytest.fixture(autouse=True)
def _clean_state():
    am._routes.clear()
    am._ring.clear()
    yield
    am._routes.clear()
    am._ring.clear()


def _raw(t: float, cpu: list[int], disk: list[int], **kw: float) -> am._Raw:
    base = dict(t=t, cpu=cpu, blocked=2, load1=1.5, disk=disk, swpin=0, swpout=0, majflt=0,
                mem_avail_kb=2048 * 1024, swap_used_kb=512 * 1024, temp_c=55.0,
                proc_jiffies=0, proc_rss_kb=1024 * 1024, proc_threads=30)
    base.update(kw)
    return am._Raw(**base)


def test_sample_turns_counter_deltas_into_rates():
    # 10 s window; cpu jiffies: user, nice, system, idle, iowait, irq, softirq, steal
    prev = _raw(0.0, [0, 0, 0, 0, 0, 0, 0, 0], [0, 0, 0, 0, 0, 0, 0], swpin=0, majflt=0)
    cur = _raw(10.0, [100, 0, 50, 200, 650, 0, 0, 0], [80, 2000, 1600, 20, 400, 200, 9000],
               swpin=50, majflt=300, proc_jiffies=250)
    s = am._sample(prev, cur)
    assert s.cpu_iowait_pct == pytest.approx(65.0)          # 650 / 1000
    assert s.cpu_busy_pct == pytest.approx(15.0)            # (1000-200-650)/1000
    assert s.disk_util_pct == pytest.approx(90.0)           # 9000 ms busy of 10 000
    assert s.disk_r_iops == pytest.approx(8.0)
    assert s.disk_w_iops == pytest.approx(2.0)
    assert s.disk_await_ms == pytest.approx((1600 + 200) / 100)
    assert s.disk_r_kbps == pytest.approx(2000 * 0.5 / 10)
    assert s.swap_in_pages_s == pytest.approx(5.0)
    assert s.major_faults_s == pytest.approx(30.0)
    assert s.proc_cpu_pct == pytest.approx(100.0 * 250 / am._CLK_TCK / 10)


def test_sample_without_disk_counters_is_zero_not_error():
    prev = _raw(0.0, [0] * 8, [])
    cur = _raw(10.0, [10, 0, 0, 90, 0, 0, 0, 0], [])
    s = am._sample(prev, cur)
    assert s.disk_util_pct == 0.0 and s.disk_await_ms == 0.0


def test_rollup_percentiles_slow_and_errors():
    for _ in range(90):
        am.record_request("gateway", "GET", "/api/sessions", 200, 8.0)
    for _ in range(9):
        am.record_request("gateway", "GET", "/api/sessions", 200, 3000.0)
    am.record_request("gateway", "GET", "/api/sessions", 500, 12000.0)
    roll = am._rollup(60.0, [1.0, 2.0, 250.0], [], am._drain_routes())
    r = roll.routes[0]
    assert (r.n, r.slow, r.err5xx) == (100, 10, 1)
    assert r.p50_ms == 10.0                    # bucket upper bound holding the median
    assert r.p95_ms == 5000.0                  # 3 s sits in the (2.5 s, 5 s] bucket
    assert r.max_ms == 12000.0
    assert (roll.req_total, roll.req_slow, roll.req_5xx) == (100, 10, 1)
    assert roll.lag_max_ms == 250.0 and roll.lag_over_100ms == 1
    assert am._routes == {}                    # window was drained


def test_quantile_is_capped_by_observed_max():
    am.record_request("gateway", "GET", "/fast", 200, 3.0)
    r = am._rollup(60.0, [], [], am._drain_routes()).routes[0]
    assert r.p50_ms == 3.0 and r.p95_ms == 3.0   # bucket bound is 5 ms, but max seen is 3 ms


def test_route_cardinality_is_capped(monkeypatch):
    monkeypatch.setattr(am, "_MAX_ROUTE_KEYS", 5)
    for i in range(50):
        am.record_request("gateway", "GET", f"/r{i}", 200, 1.0)
    assert len(am._routes) == 6                # 5 real keys + one shared overflow bucket
    assert ("gateway", "*", "overflow") in am._routes


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(am.RequestTimingMiddleware, component="t")

    @app.get("/items/{item_id}")
    def item(item_id: int) -> dict:
        return {"id": item_id}

    @app.get("/boom")
    def boom() -> dict:
        raise RuntimeError("x")

    return app


def test_middleware_keys_by_route_template_not_raw_path():
    c = TestClient(_app(), raise_server_exceptions=False)
    for i in range(3):
        assert c.get(f"/items/{i}").status_code == 200
    assert c.get("/definitely/not/a/route/abc123").status_code == 404
    assert c.get("/boom").status_code == 500
    keys = set(am._routes)
    assert ("t", "GET", "/items/{item_id}") in keys
    assert ("t", "GET", "unmatched") in keys
    assert not any("abc123" in k[2] or k[2] == "/items/1" for k in keys)
    assert am._routes[("t", "GET", "/items/{item_id}")][0] == 3
    assert am._routes[("t", "GET", "/boom")][3] == 1   # 5xx counted


def test_read_raw_on_this_host_is_sane():
    raw = am._read_raw(None)
    assert raw is not None
    assert len(raw.cpu) == 8 and raw.mem_avail_kb > 0 and raw.proc_rss_kb > 0 and raw.proc_threads >= 1


def test_sampler_produces_ring_entry_and_ndjson(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(am, "_TICK_S", 0.005)
    monkeypatch.setattr(am, "_HOST_EVERY", 2)
    monkeypatch.setattr(am, "_ROLLUP_EVERY", 6)

    async def go() -> None:
        am.record_request("gateway", "GET", "/x", 200, 5.0)
        am.start(str(tmp_path))
        for _ in range(200):
            if am._ring:
                break
            await asyncio.sleep(0.01)
        await am.stop()

    asyncio.run(go())
    assert len(am._ring) >= 1
    line = (tmp_path / "metrics.ndjson").read_text().splitlines()[0]
    data = json.loads(line)
    assert data["req_total"] == 1 and "disk_util_pct" in data["host_avg"]
    assert len(line) < 4000                       # one rollup line stays small


def test_start_is_noop_when_disabled(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("APP_METRICS_ENABLED", "false")

    async def go() -> None:
        am.start(str(tmp_path))
        assert am._task is None

    asyncio.run(go())


def test_endpoint_requires_auth_and_returns_ring(monkeypatch):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "tok")
    from src.services.session_service import SessionService
    from src.services.session_store import SessionStore

    class _Orch:
        session_service = SessionService(SessionStore(), repo_path_validator=lambda _p: None)

    c = TestClient(control_api.build_control_api(_Orch()))
    assert c.get("/api/metrics/system").status_code == 403
    am._ring.append(am._rollup(60.0, [1.0], [], {}))
    r = c.get("/api/metrics/system?minutes=5", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1 and body["rollup_seconds"] == 60
    assert c.get("/api/metrics/system?minutes=0", headers={"Authorization": "Bearer tok"}).status_code == 422


# --------------------------------------------------------------------------- health verdict

_HEALTHY = dict(cpu_busy_pct=10, cpu_iowait_pct=1, disk_util_pct=3, disk_await_ms=5,
                mem_avail_mb=4000, swap_in_pages_s=0, temp_c=55)


def _roll(host: dict | None = None, lag_p95: float = 5.0, slow: int = 0, routes=None) -> am.Rollup:
    h = {**_HEALTHY, **(host or {})}
    return am.Rollup(ts="t", window_s=60, lag_p95_ms=lag_p95, lag_max_ms=lag_p95, lag_over_100ms=0,
                     req_total=50, req_slow=slow, req_5xx=0, host_avg=h, host_max=h,
                     routes=routes or [])


def test_verdict_healthy_and_no_data():
    assert am.verdict([]).cause == "no_data" and am.verdict([]).status == "ok"
    v = am.verdict([_roll(), _roll(), _roll()])
    assert (v.status, v.cause) == ("ok", "ok")


def test_verdict_names_saturated_disk_and_links_slow_requests():
    v = am.verdict([_roll({"disk_util_pct": 97, "cpu_iowait_pct": 60, "disk_await_ms": 19}, slow=12)] * 3)
    assert (v.status, v.cause) == ("bad", "disk")
    assert "97% busy" in v.detail and "slow requests" in v.detail


def test_verdict_disk_warn_from_iowait_alone():
    v = am.verdict([_roll({"cpu_iowait_pct": 30})] * 3)
    assert (v.status, v.cause) == ("warn", "disk")


def test_verdict_memory_thermal_cpu():
    assert am.verdict([_roll({"mem_avail_mb": 250})]).cause == "memory"
    assert am.verdict([_roll({"swap_in_pages_s": 400})]).cause == "memory"
    v = am.verdict([_roll({"temp_c": 86})])
    assert (v.status, v.cause) == ("bad", "thermal")
    assert am.verdict([_roll({"cpu_busy_pct": 95})]).cause == "cpu"


def test_verdict_app_side_when_host_is_healthy():
    v = am.verdict([_roll(lag_p95=1500)] * 3)
    assert (v.status, v.cause) == ("bad", "event_loop") and "host is healthy" in v.detail
    route = am.RouteRollup(component="gateway", method="GET", route="/api/sessions", n=30,
                           p50_ms=100, p95_ms=4000, max_ms=9000, slow=9, err5xx=0)
    v = am.verdict([_roll(slow=9, routes=[route])] * 3)
    assert v.cause == "slow_routes" and "GET /api/sessions" in v.detail


def test_verdict_host_cause_outranks_app_cause():
    v = am.verdict([_roll({"disk_util_pct": 95}, lag_p95=2000, slow=20)] * 3)
    assert v.cause == "disk"


def test_verdict_only_uses_the_last_three_rollups():
    bad = _roll({"disk_util_pct": 99})
    assert am.verdict([bad, _roll(), _roll(), _roll()]).cause == "ok"


def test_health_endpoint_requires_auth_and_reports_verdict(monkeypatch):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "tok")
    from src.services.session_service import SessionService
    from src.services.session_store import SessionStore

    class _Orch:
        session_service = SessionService(SessionStore(), repo_path_validator=lambda _p: None)

    c = TestClient(control_api.build_control_api(_Orch()))
    assert c.get("/api/metrics/health").status_code == 403
    h = {"Authorization": "Bearer tok"}
    assert c.get("/api/metrics/health", headers=h).json()["cause"] == "no_data"
    am._ring.extend([_roll({"disk_util_pct": 97})] * 3)
    body = c.get("/api/metrics/health", headers=h).json()
    assert body["status"] == "bad" and body["cause"] == "disk"
    assert set(body) == {"status", "cause", "headline", "detail"}
