# Control API

The gateway exposes a single HTTP control plane (default `http://127.0.0.1:9003`).
This folder is the **self-describing map** of that surface — generated from the
running application, not hand-maintained, so it mirrors the code.

| File | What it is |
| ---- | ---------- |
| [`openapi.json`](./openapi.json) | OpenAPI 3.1 schema, emitted from the live app + a metadata overlay (auth scheme, resource tags, servers, error contract). |
| [`index.html`](./index.html) | Browsable, try-it reference ([Scalar](https://scalar.com)) rendering the spec above. |
| [`ERRORS.md`](./ERRORS.md) | Error catalog — every `reason` code and its HTTP status, generated from `_REASON_STATUS`. |

## Explore the surface

Open the reference in a browser:

```bash
python -m http.server -d docs/api 8080
# → http://127.0.0.1:8080
```

Each endpoint advertises its request body (typed Pydantic models), the resource
group it belongs to, and the uniform error responses it can return. Reading
*payload in → response out* is enough to understand what an endpoint does.

## Your first request (60 seconds)

Every `/api/*` endpoint needs a bearer token (the gateway's `DASHBOARD_TOKEN`).
`GET /health` needs none — start there:

```bash
# 1. Liveness — no auth
curl -s http://127.0.0.1:9003/health

# 2. An authenticated read
curl -s http://127.0.0.1:9003/api/sessions \
  -H "Authorization: Bearer $DASHBOARD_TOKEN"
```

Failed calls come back as a stable envelope — map the `reason`, not the prose:

```json
{ "ok": false, "reason": "session_not_found", "detail": "..." }
```

See [`ERRORS.md`](./ERRORS.md) for the full code → status table.

## Live "try it" console

The static reference above is for browsing offline. For an authenticated
request console served **same-origin** by the gateway itself, start it with
Swagger enabled:

```bash
CONTROL_API_DOCS=true python main.py
# → http://127.0.0.1:9003/docs
```

Docs are **off by default** on purpose: `/docs`, `/redoc` and `/openapi.json`
would otherwise expose the full API shape to anyone who can reach the port, with
no authentication (defense in depth). The committed spec in this folder is the
thing you browse without exposing a live surface.

## Regenerating

Whenever the routers or the reason map change, regenerate so the docs stay true:

```bash
.venv/bin/python scripts/api_docs/generate_api_docs.py
```

## Known gap (deliberate)

Request bodies are fully modelled; the shared error contract is attached to
every operation. Success **response** bodies are mostly un-modelled today
(handlers return raw `JSONResponse`) and are documented via the shared
`CommandResult` envelope rather than per-endpoint schemas. Hand-inventing those
schemas would drift from the code — adding `response_model=` to the handlers is
the honest next pass. The gap is tracked here rather than hidden.
