"""Control API routers, one module per area (see docs/backend/ARCHITECTURE.md §2).

Each module exposes ``build_router(orchestrator, *, require_auth, ...) -> APIRouter``;
``control_api.build_control_api`` builds the app, the shared auth/idempotency closures,
and includes these routers."""
