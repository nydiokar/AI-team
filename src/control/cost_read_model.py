"""A65 cost read-model — honest USD projection over the captured telemetry.

Bounded, additive, read-only. The fuel is `llm_model_requests` (per-request
tokens) joined to its turn/session; every aggregate replicates the proven
`is_duplicate=0` filter and corrects the codex `includes_cache` double-count at
the SQL layer (see `db._COST_INPUT_EXPR`). USD is always derived through
`pricing.estimate_cost` — a model absent from the price table contributes 0 to
`usd.known` and its tokens are reported as `unpriced_tokens`, so every payload
carries an honest coverage %. No fabricated numbers, ever.

Time filters use the request's own timestamp (`r.started_at`, falling back to
the turn's started_at/created_at). Attribute edges that cannot be resolved (a
turn with no session_id) surface as an `unattributed` bucket rather than being
silently dropped.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.services.pricing import TokenTotals, estimate_cost

_DISPLAY_UNKNOWN = "<unknown>"

_TOKEN_KEYS = ("input", "output", "cache_read", "cache_creation", "total")


def _price(tokens: Dict[str, Any], model: str) -> Dict[str, Any]:
    """One model's token totals priced honestly. Never fabricates a USD."""
    totals = TokenTotals(
        input=int(tokens.get("input") or 0),
        output=int(tokens.get("output") or 0),
        cache_read=int(tokens.get("cache_read") or 0),
        cache_creation=int(tokens.get("cache_creation") or 0),
    )
    totals.total = (
        totals.input + totals.output + totals.cache_read + totals.cache_creation
    )
    cost = estimate_cost(model or None, totals)
    return {
        "model": model or _DISPLAY_UNKNOWN,
        "tokens": {
            "input": totals.input,
            "output": totals.output,
            "cache_read": totals.cache_read,
            "cache_creation": totals.cache_creation,
            "total": totals.total,
        },
        "usd": {
            "known": bool(cost.known),
            "model_priced": cost.model_priced,
            "reason": cost.reason,
            "usd_total": cost.usd_total,
        },
    }


def _rollup(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Sum priced per-model rows into {tokens, usd{known, unpriced_tokens,
    coverage_pct}}. USD is summed ONLY from known estimates; unpriced models
    report their tokens so the coverage % is the honest floor of the number."""
    tokens = {k: 0 for k in _TOKEN_KEYS}
    usd_known: float = 0.0
    priced_tokens: int = 0
    unpriced_tokens: int = 0
    for row in rows:
        rt = row["tokens"]
        for k in _TOKEN_KEYS:
            tokens[k] += int(rt.get(k) or 0)
        if row["usd"]["known"]:
            usd_known += float(row["usd"]["usd_total"] or 0.0)
            priced_tokens += int(rt.get("total") or 0)
        else:
            unpriced_tokens += int(rt.get("total") or 0)
    total_tokens = priced_tokens + unpriced_tokens
    return {
        "tokens": tokens,
        "usd": {
            "known": round(usd_known, 6),
            "unpriced_tokens": unpriced_tokens,
            "coverage_pct": round(priced_tokens / total_tokens * 100, 1)
            if total_tokens
            else 100.0,
        },
    }


def _sum_aggregates(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Sum already-rolled-up bucket/session items (whose ``usd`` carries the
    aggregate shape {known, unpriced_tokens, coverage_pct}) into one total."""
    tokens = {k: 0 for k in _TOKEN_KEYS}
    usd_known: float = 0.0
    unpriced_tokens: int = 0
    for it in items:
        for k in _TOKEN_KEYS:
            tokens[k] += int(it["tokens"].get(k) or 0)
        usd_known += float(it["usd"].get("known") or 0.0)
        unpriced_tokens += int(it["usd"].get("unpriced_tokens") or 0)
    total_tokens = sum(tokens.values())
    priced_tokens = max(total_tokens - unpriced_tokens, 0)
    return {
        "tokens": tokens,
        "usd": {
            "known": round(usd_known, 6),
            "unpriced_tokens": unpriced_tokens,
            "coverage_pct": round(priced_tokens / total_tokens * 100, 1)
            if total_tokens
            else 100.0,
        },
    }


def _dominant_models(rows: List[Dict[str, Any]], limit: int = 3) -> List[Dict[str, Any]]:
    """The top models by known USD (then tokens) inside a bucket — so the UI can
    state WHICH model drove a bucket, the operator's explicit ask."""
    priced = sorted(
        (r for r in rows if r["usd"]["known"]),
        key=lambda r: (
            -float(r["usd"]["usd_total"] or 0.0),
            -int(r["tokens"].get("total") or 0),
        ),
    )
    unpriced = sorted(
        (r for r in rows if not r["usd"]["known"]),
        key=lambda r: -int(r["tokens"].get("total") or 0),
    )
    return [
        {
            "model": r["model"],
            "usd_total": r["usd"]["usd_total"],
            "known": r["usd"]["known"],
            "reason": r["usd"]["reason"],
            "tokens_total": r["tokens"]["total"],
        }
        for r in (priced + unpriced)[:limit]
    ]


def assemble_explorer(
    db: Any,
    *,
    dimension: str,
    granularity: str = "day",
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
    repo_path: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """Spend over time / by dimension. One bounded GROUP BY, priced in Python.

    Response: series rows per (bucket, dim) with token buckets + known USD +
    coverage + the dominant models; a whole-window totals rollup; and the
    unattributed bucket (turns with no session_id) so nothing is hidden.
    """
    rows = db.cost_usage_rows(
        dimension=dimension,
        granularity=granularity,
        from_ts=from_ts,
        to_ts=to_ts,
        repo_path=repo_path,
        limit=limit * 20,
    )
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for r in rows:
        key = (r.get("bucket") or "", r.get("dim") or _DISPLAY_UNKNOWN)
        groups.setdefault(key, []).append(_price(r, r.get("model") or ""))
    series: List[Dict[str, Any]] = []
    for (bucket, dim), priced_rows in groups.items():
        item = _rollup(priced_rows)
        item.update({"bucket": bucket, "dim": dim})
        item["models"] = _dominant_models(priced_rows)
        series.append(item)
    series.sort(
        key=lambda s: (
            s["bucket"],
            -float(s["usd"]["known"] or 0.0),
        )
    )
    top = max(limit, 1)
    series = series[: top * 40]
    totals = _sum_aggregates(series)
    unattributed = _price_bucket(
        db.cost_unattributed(from_ts=from_ts, to_ts=to_ts)
    )
    return {
        "ok": True,
        "dimension": dimension,
        "granularity": granularity,
        "from": from_ts,
        "to": to_ts,
        "repo_path": repo_path,
        "series": series,
        "totals": totals,
        "unattributed": unattributed,
    }


def _price_bucket(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Price a per-model row set (used for the unattributed bucket)."""
    if not rows:
        return {
            "tokens": {k: 0 for k in _TOKEN_KEYS},
            "usd": {"known": 0.0, "unpriced_tokens": 0, "coverage_pct": 100.0},
            "models": [],
        }
    priced_rows = [_price(r, r.get("model") or "") for r in rows]
    out = _rollup(priced_rows)
    out["models"] = _dominant_models(priced_rows)
    return out


def assemble_case_usage(
    db: Any, flow_run_id: str
) -> Optional[Dict[str, Any]]:
    """The operator-question endpoint: manager + workers, each session's tokens
    and USD, plus the mgr_vs_workers summary. Returns None when the case (or its
    flow run) is unknown — the caller maps that to 404."""
    flow = db.get_flow_run(flow_run_id)
    if flow is None:
        return None
    links = db.list_flow_links(flow_run_id=flow_run_id, entity_type="session")
    role_by_session: Dict[str, str] = {}
    for link in links or []:
        sid = link.get("entity_id")
        if sid and sid not in role_by_session:
            role_by_session[sid] = link.get("role") or "member"
    rows = db.cost_case_rows(flow_run_id)
    per_session: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        per_session.setdefault(r["session_id"], []).append(
            _price(r, r.get("model") or "")
        )
    session_rows: List[Dict[str, Any]] = []
    manager_rows: List[Dict[str, Any]] = []
    worker_rows: List[Dict[str, Any]] = []
    for sid, priced_rows in sorted(per_session.items()):
        item = _rollup(priced_rows)
        item.update({
            "session_id": sid,
            "role": role_by_session.get(sid, "member"),
            "models": _dominant_models(priced_rows),
        })
        session_rows.append(item)
        if item["role"] == "manager":
            manager_rows.append(item)
        elif item["role"] == "worker":
            worker_rows.append(item)
    mgr = _sum_aggregates(manager_rows)
    wk = _sum_aggregates(worker_rows)
    both = _sum_aggregates(session_rows)
    workers_share = None
    total_usd = float(mgr["usd"]["known"] or 0.0) + float(wk["usd"]["known"] or 0.0)
    if total_usd > 0:
        workers_share = round(float(wk["usd"]["known"] or 0.0) / total_usd * 100, 1)
    return {
        "ok": True,
        "flow_run_id": flow_run_id,
        "case": {
            "status": flow.get("status"),
            "objective_lock": flow.get("objective_lock"),
            "created_at": flow.get("created_at"),
        },
        "sessions": session_rows,
        "mgr_vs_workers": {
            "manager": mgr,
            "workers": wk,
            "workers_share_pct": workers_share,
            "worker_sessions": len(worker_rows),
        },
        "totals": both,
    }


def assemble_top_sessions(
    db: Any,
    *,
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
    repo_path: Optional[str] = None,
    by: str = "usd",
    limit: int = 10,
) -> Dict[str, Any]:
    """Top consumers (big-company top spenders). Per-session USD is rolled up
    from per-model pricing so a session mixing models prices correctly; every row
    carries its dominant model + coverage so an unpriced big burner is visible,
    not silent."""
    rows = db.cost_session_model_rows(
        from_ts=from_ts, to_ts=to_ts, repo_path=repo_path, limit=limit * 50
    )
    per_session: Dict[str, List[Dict[str, Any]]] = {}
    meta: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        per_session.setdefault(r["session_id"], []).append(
            _price(r, r.get("model") or "")
        )
        meta[r["session_id"]] = {
            "repo_path": r.get("repo_path"),
            "backend": r.get("backend"),
            "role": r.get("role"),
        }
    entries: List[Dict[str, Any]] = []
    for sid, priced_rows in per_session.items():
        item = _rollup(priced_rows)
        item.update({
            "session_id": sid,
            "models": _dominant_models(priced_rows),
        })
        m = meta[sid]
        item["repo_path"] = m.get("repo_path")
        item["backend"] = m.get("backend")
        item["role"] = m.get("role")
        entries.append(item)
    if by == "tokens":
        entries.sort(key=lambda e: -int(e["tokens"]["total"] or 0))
    else:  # usd (known) — the honest spend sort
        entries.sort(
            key=lambda e: (
                -float(e["usd"]["known"] or 0.0),
                -int(e["tokens"]["total"] or 0),
            )
        )
    entries = entries[: max(1, min(int(limit), 100))]
    totals = _sum_aggregates(entries)
    return {"ok": True, "by": by, "limit": len(entries), "rows": entries, "totals": totals}


def assemble_projects(
    db: Any,
    *,
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """Project list + per-project rollup — the Cost-tab project-filter fuel.

    Token-only rollup: a project is not a model, so there is no per-project USD
    here (USD is per model; the explorer's ``dimension=project`` prices that).
    The dropdown needs the list + relative weight, which tokens provide."""
    rows = db.list_cost_projects(from_ts=from_ts, to_ts=to_ts, limit=limit)
    projects: List[Dict[str, Any]] = []
    for r in rows:
        tokens = {
            k: int(r.get(k) or 0)
            for k in ("input", "output", "cache_read", "cache_creation")
        }
        tokens["total"] = sum(tokens.values())
        projects.append({
            "repo_path": r["repo_path"],
            "tokens": tokens,
        })
    return {"ok": True, "limit": len(projects), "projects": projects}
