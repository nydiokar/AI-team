"""Every Control API route requires the Bearer token unless it is on an explicit allowlist.

Auth is declared two ways across ``src/control/routes/`` — router-level in the
all-authenticated areas, per-route in the mixed ones — so a new route added to a mixed
router without ``dependencies=[Depends(require_auth)]`` would be silently public. This
pins the public surface to the three routes that own their own auth.
"""

import types

from fastapi.routing import iter_route_contexts

from src.control import control_api

# (method, path) -> why it is not behind _require_auth.
PUBLIC_ROUTES = {
    ("GET", "/health"): "liveness probe",
    ("GET", "/api/events/stream"): "SSE; authenticates the token in the handler",
    ("POST", "/api/sessions/{session_id}/turn-requests"): "operator Bearer OR agent-sender scope",
}


def _dep_calls(dependant) -> set:
    out = set()
    for d in dependant.dependencies:
        out.add(d.call)
        out |= _dep_calls(d)
    return out


def test_every_api_route_requires_auth_except_allowlist():
    app = control_api.build_control_api(types.SimpleNamespace())
    public = set()
    for ctx in iter_route_contexts(app.routes):
        dependant = getattr(ctx.route, "dependant", None)
        if dependant is None:  # static mounts (web UI) carry no dependant
            continue
        if not any(getattr(c, "__name__", "") == "_require_auth" for c in _dep_calls(dependant)):
            public |= {(m, ctx.path) for m in ctx.methods - {"HEAD"}}
    assert public == set(PUBLIC_ROUTES)
