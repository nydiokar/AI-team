"""A65 cost read-model: pricing, DB aggregation, read-model assemblers, and the
Control API surface. No paid backend — pure SQL + math over an isolated MeshDB
(conftest), so it is cheap and safe under the TEST COST GUARD."""
import pytest
from fastapi.testclient import TestClient

from src.control import control_api
from src.control.db import MeshDB, get_db
from src.services.pricing import TokenTotals, estimate_cost

TOKEN = "test-control-token"

_MONO = "2026-07-01T09:00:00+00:00"


def _seed(db: MeshDB) -> None:
    """Seed two projects, a case (manager+worker), a duplicate, an unattributed
    turn, and a standalone session — the full read-model surface."""
    con = db._conn()
    con.execute(
        "INSERT INTO sessions (session_id, backend, repo_path, status, created_at,"
        " updated_at, machine_id, backend_session_id, last_task_id, last_artifact_path,"
        " last_summary, last_user_message, last_result_summary, last_files_modified,"
        " task_history, origin, driver_type, driver_status, cache_health,"
        " cache_unhealthy_count, previous_backend_session_ids, current_case_id, case_role)"
        " VALUES ('mgr-1','codex','/proj/a','active','t0','t0','m','b','t','','','','','',"
        " '[]','telegram','','','',0,'[]','case-0000000000000001','manager'),"
        "       ('wk-1','codex','/proj/a','active','t0','t0','m','b','t','','','','','',"
        " '[]','telegram','','','',0,'[]','case-0000000000000001','worker'),"
        "       ('standalone-1','claude','/proj/b','active','t0','t0','m','b','t','','','','','',"
        " '[]','telegram','','','',0,'[]',NULL,'')"
    )
    con.execute(
        "INSERT INTO flow_runs (flow_run_id, task_id, created_at, updated_at, objective_lock)"
        " VALUES ('case-0000000000000001','t','t0','t0','seed')"
    )
    con.execute(
        "INSERT INTO flow_links (flow_run_id, entity_type, entity_id, role, created_at)"
        " VALUES ('case-0000000000000001','session','mgr-1','manager','t0'),"
        "        ('case-0000000000000001','session','wk-1','worker','t0')"
    )
    con.execute(
        "INSERT INTO llm_turns (turn_id, session_id, task_id, backend, requested_model,"
        " observed_models, started_at, ended_at, final_status, timeout_status,"
        " metrics_json, coverage_json, data_quality_json, projection_version,"
        " created_at, updated_at)"
        " VALUES ('turn-1','mgr-1','t','codex','gpt-5.6-terra','[\"gpt-5.6-terra\"]',"
        "  '2026-08-03T10:00:00+00:00','2026-08-03T10:00:30+00:00','success','none',"
        "  '{}','{}','{}',1,'t0','t0'),"
        "       ('turn-2','mgr-1','t','codex','gpt-5.6-terra','[\"gpt-5.6-terra\"]',"
        "  '2026-08-03T10:00:30+00:00','2026-08-03T10:01:00+00:00','success','none',"
        "  '{}','{}','{}',1,'t0','t0'),"
        "       ('turn-3','wk-1','t','codex','opus','[\"opus\"]',"
        "  '2026-08-03T12:00:00+00:00','2026-08-03T12:00:30+00:00','success','none',"
        "  '{}','{}','{}',1,'t0','t0'),"
        "       ('turn-4',NULL,'t','codex','gpt-5.5','[\"gpt-5.5\"]',"
        "  '2026-07-01T09:00:00+00:00','2026-07-01T09:00:30+00:00','success','none',"
        "  '{}','{}','{}',1,'t0','t0'),"
        "       ('turn-5','standalone-1','t','claude','sonnet','[\"sonnet\"]',"
        "  '2026-07-01T11:00:00+00:00','2026-07-01T11:00:30+00:00','success','none',"
        "  '{}','{}','{}',1,'t0','t0')"
    )
    con.execute(
        "INSERT INTO llm_invocations (invocation_id, turn_id, attempt, spawn_reason, action,"
        " node_id, backend, status, usage_json, coverage_json, data_quality_json)"
        " VALUES ('inv-1','turn-1',0,'seed','run','m','codex','success','{}','{}','{}'),"
        "        ('inv-2','turn-2',0,'seed','run','m','codex','success','{}','{}','{}'),"
        "        ('inv-3','turn-3',0,'seed','run','m','codex','success','{}','{}','{}'),"
        "        ('inv-4','turn-4',0,'seed','run','m','codex','success','{}','{}','{}'),"
        "        ('inv-5','turn-5',0,'seed','run','m','claude','success','{}','{}','{}')"
    )
    con.execute(
        "INSERT INTO llm_model_requests (model_request_id, invocation_id, turn_id, sequence,"
        " model, work_category, started_at, ended_at, status, input_tokens, output_tokens,"
        " cache_read_tokens, cache_creation_tokens, reasoning_tokens, context_tokens,"
        " input_token_semantics, usage_granularity, usage_source, usage_coverage,"
        " is_duplicate, data_quality_json)"
        " VALUES ('req-1','inv-1','turn-1',1,'gpt-5.6-terra','code_generation',"
        "  '2026-08-03T10:00:00+00:00','2026-08-03T10:00:30+00:00','success',"
        "  1000000,100000,800000,50000,0,0,'includes_cache','turn_total',"
        "  'codex.rollout.token_count.last_token_usage','aggregate_only',0,'{}'),"
        "       ('req-2','inv-2','turn-2',1,'gpt-5.6-terra','code_generation',"
        "  '2026-08-03T10:00:30+00:00','2026-08-03T10:01:00+00:00','success',"
        "  1000000,100000,800000,50000,0,0,'includes_cache','turn_total',"
        "  'codex.rollout.token_count.last_token_usage','aggregate_only',1,'{}'),"
        "       ('req-3','inv-3','turn-3',1,'opus','code_generation',"
        "  '2026-08-03T12:00:00+00:00','2026-08-03T12:00:30+00:00','success',"
        "  500000,200000,1000000,100000,0,0,'exclusive','turn_total',"
        "  'claude.result.usage','aggregate_only',0,'{}'),"
        "       ('req-4','inv-4','turn-4',1,'gpt-5.5','code_generation',"
        "  '2026-07-01T09:00:00+00:00','2026-07-01T09:00:30+00:00','success',"
        "  100000,10000,0,0,0,0,'includes_cache','turn_total',"
        "  'turn.completed.usage','aggregate_only',0,'{}'),"
        "       ('req-5','inv-5','turn-5',1,'sonnet','code_generation',"
        "  '2026-07-01T11:00:00+00:00','2026-07-01T11:00:30+00:00','success',"
        "  300000,100000,0,0,0,0,'exclusive','turn_total',"
        "  'claude.result.usage','aggregate_only',0,'{}')"
    )


@pytest.fixture
def seeded_db():
    db = get_db()
    assert db is not None
    _seed(db)
    return db


# --- pricing (the A65 table additions) ---------------------------------------

def test_gpt56_rates():
    t = TokenTotals(input=1_000_000, output=1_000_000, cache_read=1_000_000,
                    cache_creation=1_000_000, total=4_000_000)
    est = estimate_cost("gpt-5.6-terra", t)
    assert est.known is True
    assert (est.usd_input, est.usd_output) == (2.0, 12.0)
    est_luna = estimate_cost("gpt-5.6-luna", t)
    assert est_luna.known is True
    assert est_luna.usd_input == 0.2
    assert est_luna.usd_total == round(0.2 + 1.2 + 0.25 + 0.02, 6)


def test_gpt55_and_codex_rates():
    t = TokenTotals(input=1_000_000, output=1_000_000, cache_read=0,
                    cache_creation=0, total=2_000_000)
    est = estimate_cost("gpt-5.5", t)
    assert est.known is True and est.usd_total == 35.0
    est_codex = estimate_cost("gpt-5.3-codex", t)
    assert est_codex.known is True
    assert est_codex.usd_total == round(1.75 + 14.0, 6)


def test_fable_rates():
    t = TokenTotals(input=1_000_000, output=1_000_000, cache_read=0,
                    cache_creation=0, total=2_000_000)
    est = estimate_cost("fable", t)
    assert est.known is True
    assert (est.usd_input, est.usd_output) == (10.0, 50.0)


def test_longest_key_match_preferred():
    """'gpt-5.5' must never price 'gpt-5.5-pro'/'gpt-5.6-terra' must not match
    the plain 'gpt-5.6' fallback; longest matching key wins."""
    t = TokenTotals(input=1_000_000, output=0, cache_read=0, cache_creation=0, total=1_000_000)
    assert estimate_cost("gpt-5.5-pro", t).usd_input == 5.0
    assert estimate_cost("gpt-5.6-terra-xlarge", t).usd_input == 2.0
    assert estimate_cost("gpt-5.6-sol", t).usd_input == 5.0


def test_gpt5_codex_without_5_3_still_unknown():
    t = TokenTotals(input=1_000_000, output=0, cache_read=0, cache_creation=0, total=1_000_000)
    est = estimate_cost("gpt-5-codex", t)
    assert est.known is False and est.reason == "unknown_model_pricing"


# --- get_session_token_totals (double-count fix + total definition) ----------

def test_session_totals_normalize_inclusive_cache(seeded_db):
    out = seeded_db.get_session_token_totals(["mgr-1"])
    d = out["mgr-1"]
    # includes_cache: input (1M) minus cache_read (800k) = 200k; the duplicate
    # request (is_duplicate=1) must NOT double the counts.
    assert d["input"] == 200_000
    assert d["output"] == 100_000
    assert d["cache_read"] == 800_000
    assert d["cache_creation"] == 50_000
    assert d["total"] == 200_000 + 100_000 + 800_000 + 50_000


def test_session_totals_leave_exclusive_cache_untouched(seeded_db):
    d = seeded_db.get_session_token_totals(["wk-1"])["wk-1"]
    assert d["input"] == 500_000  # claude.result.usage: exclusive, no subtraction
    assert d["total"] == 500_000 + 200_000 + 1_000_000 + 100_000


# --- read-model assemblers ----------------------------------------------------

def test_explorer_model_dimension_prices_per_model(seeded_db):
    from src.control.cost_read_model import assemble_explorer
    out = assemble_explorer(seeded_db, dimension="model", granularity="", limit=10)
    by_model = {s["dim"]: s for s in out["series"]}
    assert by_model["opus"]["usd"]["coverage_pct"] == 100.0
    assert by_model["opus"]["usd"]["known"] == pytest.approx(8.625)
    assert by_model["gpt-5.6-terra"]["tokens"]["input"] == 200_000
    assert by_model["sonnet"]["usd"]["known"] == pytest.approx(2.4)
    assert out["totals"]["usd"]["known"] == pytest.approx(8.625 + 1.885 + 2.4)


def test_explorer_unattributed_surfaced_honestly(seeded_db):
    from src.control.cost_read_model import assemble_explorer
    out = assemble_explorer(seeded_db, dimension="model", granularity="", limit=10)
    assert out["unattributed"]["tokens"]["total"] == 110_000
    assert out["unattributed"]["usd"]["known"] == pytest.approx(0.8)


def test_explorer_day_buckets_and_window_filter(seeded_db):
    from src.control.cost_read_model import assemble_explorer
    out = assemble_explorer(seeded_db, dimension="model", granularity="day", limit=10)
    buckets = {s["bucket"] for s in out["series"]}
    assert buckets == {"2026-08-03", "2026-07-01"}
    out2 = assemble_explorer(seeded_db, dimension="model", granularity="day",
                             from_ts="2026-08-03T11:00:00+00:00", to_ts="2026-08-03T23:00:00+00:00")
    assert {s["bucket"] for s in out2["series"]} == {"2026-08-03"}
    assert {s["dim"] for s in out2["series"]} == {"opus"}


def test_explorer_repo_path_filter(seeded_db):
    from src.control.cost_read_model import assemble_explorer
    out = assemble_explorer(seeded_db, dimension="model", granularity="",
                            repo_path="/proj/a", limit=10)
    assert {s["dim"] for s in out["series"]} == {"gpt-5.6-terra", "opus"}


def test_case_usage_manager_workers_split(seeded_db):
    from src.control.cost_read_model import assemble_case_usage
    out = assemble_case_usage(seeded_db, "case-0000000000000001")
    assert out["ok"] is True
    m = out["mgr_vs_workers"]
    assert m["manager"]["tokens"]["total"] == 1_150_000
    assert m["manager"]["usd"]["known"] == pytest.approx(1.885)
    assert m["workers"]["tokens"]["total"] == 1_800_000
    assert m["workers"]["usd"]["known"] == pytest.approx(8.625)
    assert m["worker_sessions"] == 1
    assert m["workers_share_pct"] == pytest.approx(82.1)
    roles = {s["session_id"]: s["role"] for s in out["sessions"]}
    assert roles == {"mgr-1": "manager", "wk-1": "worker"}


def test_case_usage_unknown_case_is_none(seeded_db):
    from src.control.cost_read_model import assemble_case_usage
    assert assemble_case_usage(seeded_db, "no-such-case") is None


def test_top_sessions_ranked_by_usd(seeded_db):
    from src.control.cost_read_model import assemble_top_sessions
    out = assemble_top_sessions(seeded_db, by="usd", limit=10)
    usds = [r["usd"]["known"] for r in out["rows"]]
    assert usds == sorted(usds, reverse=True)
    assert usds[0] == pytest.approx(8.625)  # the worker (opus) is the top spender
    assert usds[1] == pytest.approx(2.4)
    assert usds[2] == pytest.approx(1.885)
    assert out["totals"]["usd"]["known"] == pytest.approx(sum(usds))


def test_top_sessions_tokens_sort(seeded_db):
    from src.control.cost_read_model import assemble_top_sessions
    out = assemble_top_sessions(seeded_db, by="tokens", limit=10)
    toks = [r["tokens"]["total"] for r in out["rows"]]
    assert toks == sorted(toks, reverse=True)


def test_projects_list(seeded_db):
    from src.control.cost_read_model import assemble_projects
    out = assemble_projects(seeded_db, limit=10)
    by_path = {p["repo_path"]: p for p in out["projects"]}
    assert set(by_path) == {"/proj/a", "/proj/b"}
    assert by_path["/proj/a"]["tokens"]["total"] == 1_150_000 + 1_800_000
    assert by_path["/proj/b"]["tokens"]["total"] == 400_000


# --- Control API surface ------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    return TestClient(control_api.build_control_api(_StubOrchestrator()))


class _StubOrchestrator:
    def __init__(self) -> None:
        self.session_service = None
        self.quota_coordinator = None


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def test_cost_endpoints_require_auth(client):
    for path in ("/api/cost/explorer", "/api/cost/top", "/api/cost/projects",
                 "/api/cases/x/usage"):
        assert client.get(path).status_code in (401, 403)


def test_explorer_endpoint_roundtrip(client, seeded_db):
    r = client.get("/api/cost/explorer", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["dimension"] == "project"
    assert {s["dim"] for s in body["series"]} == {"/proj/a", "/proj/b"}


def test_explorer_unknown_dimension_422(client):
    r = client.get("/api/cost/explorer?dimension=bogus", headers=_auth())
    assert r.status_code == 422


def test_top_endpoint_and_tokens_sort(client, seeded_db):
    r = client.get("/api/cost/top?by=tokens", headers=_auth())
    assert r.status_code == 200
    assert r.json()["by"] == "tokens"


def test_top_unknown_by_422(client):
    assert client.get("/api/cost/top?by=nope", headers=_auth()).status_code == 422


def test_case_usage_endpoint(client, seeded_db):
    r = client.get("/api/cases/case-0000000000000001/usage", headers=_auth())
    assert r.status_code == 200
    m = r.json()["mgr_vs_workers"]
    assert m["worker_sessions"] == 1
    assert m["workers_share_pct"] == pytest.approx(82.1)


def test_case_usage_unknown_404(client, seeded_db):
    r = client.get("/api/cases/no-such-case/usage", headers=_auth())
    assert r.status_code == 404


def test_projects_endpoint(client, seeded_db):
    r = client.get("/api/cost/projects", headers=_auth())
    assert r.status_code == 200
    paths = {p["repo_path"] for p in r.json()["projects"]}
    assert paths == {"/proj/a", "/proj/b"}
