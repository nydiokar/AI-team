"""Cockpit M3 — read-only web dashboard (FastAPI app).

A second surface beside Telegram, built **entirely on the M1/M2 contract** with
no core change:

  * **State** comes from the read model — ``SessionService.list_views()`` (M2
    SessionView DTOs) and ``db.list_tasks/list_nodes`` (docs/CONTROL_CONTRACT.md §6).
  * **Live deltas** come from the event stream — ``observability.read_recent_events``
    tails ``logs/events.ndjson`` (§1); the client polls ``/api/events?since=<offset>``.
    Per the contract, events are *not* replayed for gap recovery — the client
    refreshes state from the read endpoints instead (§1).

It is **read-only**: it issues no inbound commands, writes no session state, and
has no forms (so it sidesteps the optional ``python-multipart`` dependency the
task server's upload endpoints need). When a future write surface is wanted, it
calls ``SessionService`` / ``submit_instruction`` (§4) — not this module.

Locally:
    uvicorn src.control.dashboard:app --host 127.0.0.1 --port 9003

All ``/api/*`` endpoints require ``Authorization: Bearer {DASHBOARD_TOKEN}``
(falls back to ``WORKER_TOKEN``). The HTML shell at ``/`` is unauthenticated but
carries no data; it fetches the APIs with a token the operator supplies.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.core import observability

logger = logging.getLogger(__name__)

app = FastAPI(title="AI-Team Cockpit Dashboard", version="1.0")

_bearer = HTTPBearer(auto_error=True)


# ---------------------------------------------------------------------------
# Auth — reuse the mesh secret; dashboard-specific override allowed
# ---------------------------------------------------------------------------

def _dashboard_token() -> str:
    try:
        from config import config as _cfg
        return _cfg.mesh.dashboard_token or _cfg.mesh.worker_token
    except Exception:
        import os
        return os.getenv("DASHBOARD_TOKEN", "") or os.getenv("WORKER_TOKEN", "")


def _require_auth(creds: HTTPAuthorizationCredentials = Security(_bearer)) -> None:
    token = _dashboard_token()
    if not token:
        raise HTTPException(status_code=500, detail="DASHBOARD_TOKEN not configured")
    if creds.credentials != token:
        raise HTTPException(status_code=401, detail="Invalid token")


# ---------------------------------------------------------------------------
# Read-model accessors (lazy — keep import side-effect-free / testable)
# ---------------------------------------------------------------------------

_session_service = None  # lazily built once; the store holds no per-request state


def _get_session_service():
    global _session_service
    if _session_service is None:
        from src.services.session_store import SessionStore
        from src.services.session_service import SessionService
        _session_service = SessionService(SessionStore())
    return _session_service


def _session_views(limit: int) -> List[Dict[str, Any]]:
    """SessionView dicts via the shared store (DB-first). Empty on any failure."""
    try:
        return [v.to_dict() for v in _get_session_service().list_views(limit=limit)]
    except Exception as e:
        logger.warning("dashboard_sessions_failed err=%s", e)
        return []


def _db():
    try:
        from src.control.db import get_db
        return get_db()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# JSON API (read-only)
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/api/sessions", dependencies=[Depends(_require_auth)])
def api_sessions(limit: int = Query(200, ge=1, le=1000)) -> JSONResponse:
    return JSONResponse({"sessions": _session_views(limit)})


@app.get("/api/tasks", dependencies=[Depends(_require_auth)])
def api_tasks(limit: int = Query(50, ge=1, le=500)) -> JSONResponse:
    db = _db()
    tasks = db.list_tasks(limit=limit) if db is not None else []
    return JSONResponse({"tasks": tasks})


@app.get("/api/nodes", dependencies=[Depends(_require_auth)])
def api_nodes() -> JSONResponse:
    db = _db()
    nodes = db.list_nodes() if db is not None else []
    for n in nodes:
        _annotate_node_liveness(n)
    return JSONResponse({"nodes": nodes})


def _heartbeat_timeout_sec() -> int:
    try:
        from config import config as _cfg
        return int(_cfg.mesh.node_heartbeat_timeout_sec)
    except Exception:
        return 90


def _annotate_node_liveness(node: Dict[str, Any]) -> None:
    """Derive a fresh ``live`` flag + ``heartbeat_age_sec`` from ``last_heartbeat``.

    The stored ``status`` column is flipped to "offline" by the NodeRegistry's
    expiry loop, which runs only in the gateway/server process — NOT here. So the
    dashboard (a separate, read-only process) must derive liveness from the
    heartbeat timestamp itself, using the same timeout the registry uses, instead
    of trusting a column another process may not have updated yet.
    """
    from datetime import datetime, timezone  # noqa: F401  (timezone used below)

    node["live"] = False
    node["heartbeat_age_sec"] = None
    raw = node.get("last_heartbeat")
    if not raw:
        return
    try:
        hb = datetime.fromisoformat(str(raw))
        # DB timestamps (db._now -> datetime.utcnow().isoformat()) are naive but
        # represent UTC. Normalise both sides to UTC so the age is correct
        # regardless of the dashboard host's local timezone — otherwise every node
        # reads "offline" by the local/UTC offset.
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        age = (now - hb).total_seconds()
        node["heartbeat_age_sec"] = round(age, 1)
        node["live"] = age <= _heartbeat_timeout_sec()
    except Exception:
        # Unparseable timestamp -> leave live=False, age=None (treated as offline).
        return


@app.get("/api/events", dependencies=[Depends(_require_auth)])
def api_events(
    since: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
) -> JSONResponse:
    """Live event deltas. Pass the returned ``offset`` back as ``since`` to poll.

    ``since=0`` returns the tail of the stream (cold start); a non-zero offset
    returns only what was appended since. Gap recovery is NOT a replay — the
    client refreshes state from /api/sessions|tasks|nodes (CONTROL_CONTRACT §1).
    """
    data = observability.read_recent_events(limit=limit, since_offset=since)
    return JSONResponse(data)


# ---------------------------------------------------------------------------
# HTML shell — carries no data; fetches the APIs client-side
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML)


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>AI-Team Cockpit</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; margin: 0;
         background:#0d1117; color:#c9d1d9; font-size:13px; }
  header { padding:10px 16px; background:#161b22; border-bottom:1px solid #30363d;
           display:flex; gap:12px; align-items:center; }
  header h1 { font-size:14px; margin:0; font-weight:600; }
  header input { background:#0d1117; color:#c9d1d9; border:1px solid #30363d;
                 padding:4px 8px; border-radius:6px; width:280px; }
  #status { margin-left:auto; color:#8b949e; }
  main { display:grid; grid-template-columns:1fr 1fr; gap:12px; padding:12px; }
  section { background:#161b22; border:1px solid #30363d; border-radius:8px;
            padding:10px; overflow:auto; max-height:46vh; }
  section.wide { grid-column:1 / -1; max-height:30vh; }
  h2 { font-size:12px; text-transform:uppercase; letter-spacing:.05em;
       color:#8b949e; margin:0 0 8px; }
  table { width:100%; border-collapse:collapse; }
  th,td { text-align:left; padding:3px 6px; border-bottom:1px solid #21262d;
          white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:220px; }
  th { color:#8b949e; font-weight:500; }
  .pill { padding:1px 6px; border-radius:10px; font-size:11px; }
  .ok{background:#238636;color:#fff}.busy{background:#9e6a03;color:#fff}
  .err{background:#da3633;color:#fff}.idle{background:#30363d;color:#c9d1d9}
  #events div { padding:2px 0; border-bottom:1px solid #21262d; }
  .ev-name { color:#58a6ff; }
  .muted{color:#8b949e}
</style>
</head>
<body>
<header>
  <h1>AI-Team Cockpit</h1>
  <input id="token" type="password" placeholder="DASHBOARD_TOKEN" />
  <span id="status">enter token to connect</span>
</header>
<main>
  <section><h2>Sessions</h2><table id="sessions"></table></section>
  <section><h2>Nodes</h2><table id="nodes"></table></section>
  <section><h2>Tasks</h2><table id="tasks"></table></section>
  <section class="wide"><h2>Live events</h2><div id="events"></div></section>
</main>
<script>
let token = localStorage.getItem("dash_token") || "";
let offset = 0;
const $ = (id) => document.getElementById(id);
$("token").value = token;
$("token").addEventListener("change", e => {
  token = e.target.value.trim();
  localStorage.setItem("dash_token", token);
  offset = 0; $("events").innerHTML = ""; tick();
});

async function api(path) {
  const r = await fetch(path, { headers: { Authorization: "Bearer " + token } });
  if (!r.ok) throw new Error(r.status + " " + r.statusText);
  return r.json();
}
function statusPill(s) {
  const k = (s||"").toLowerCase();
  const cls = k==="busy"?"busy":["error","cancelled"].includes(k)?"err":
              k==="closed"?"idle":k==="idle"||k==="awaiting_input"?"ok":"idle";
  return `<span class="pill ${cls}">${s||""}</span>`;
}
function rows(el, headers, data, cells) {
  el.innerHTML = "<tr>" + headers.map(h=>`<th>${h}</th>`).join("") + "</tr>" +
    data.map(d => "<tr>" + cells(d).map(c=>`<td>${c}</td>`).join("") + "</tr>").join("");
}
function esc(x){ return String(x==null?"":x).replace(/[&<>]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }

async function refreshState() {
  const [s, n, t] = await Promise.all([api("/api/sessions"), api("/api/nodes"), api("/api/tasks?limit=50")]);
  rows($("sessions"), ["id","backend","status","model","repo"], s.sessions,
    d => [esc(d.session_id), esc(d.backend), statusPill(d.status), esc(d.model||"—"),
          `<span class="muted">${esc(d.repo_path)}</span>`]);
  rows($("nodes"), ["node","live","backends","last hb"], n.nodes,
    d => [esc(d.node_id),
          `<span class="pill ${d.live?"ok":"err"}">${d.live?"live":"offline"}</span>`,
          esc((d.backends||"")).slice(0,40),
          d.heartbeat_age_sec==null?'<span class="muted">—</span>'
            :`<span class="muted">${Math.round(d.heartbeat_age_sec)}s</span>`]);
  rows($("tasks"), ["id","status","session"], t.tasks,
    d => [esc(d.task_id||d.id), statusPill(d.status), esc(d.session_id)]);
}
async function refreshEvents() {
  const data = await api("/api/events?since=" + offset);
  offset = data.offset || offset;
  const box = $("events");
  for (const ev of data.events) {
    const div = document.createElement("div");
    div.innerHTML = `<span class="muted">${esc((ev.timestamp||"").slice(11,19))}</span> ` +
      `<span class="ev-name">${esc(ev.event)}</span> ` +
      `<span class="muted">${esc(ev.session_id||ev.task_id||"")}</span>`;
    box.prepend(div);
  }
  while (box.children.length > 200) box.removeChild(box.lastChild);
}
async function tick() {
  if (!token) { $("status").textContent = "enter token to connect"; return; }
  try {
    await refreshState();
    await refreshEvents();
    $("status").textContent = "connected · " + new Date().toLocaleTimeString();
  } catch (e) {
    $("status").textContent = "error: " + e.message;
  }
}
tick();
setInterval(tick, 3000);
</script>
</body>
</html>
"""
