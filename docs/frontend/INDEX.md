# Frontend Docs Index

Docs for `web/` — the mobile Web UI surface of the AI-team gateway. For the
project as a whole, start at [`.ai/CONTEXT.md`](../../.ai/CONTEXT.md); for the
full `docs/` catalog see [`docs/INDEX.md`](../INDEX.md).

| Doc | Read for |
|---|---|
| [`OVERVIEW.md`](OVERVIEW.md) | **Start here.** Stack, why the domain/transport/adapter layering exists, directory map, state model (server/live/client), routing. |
| [`DATA_FLOW.md`](DATA_FLOW.md) | How data actually moves — polling vs. SSE, the two-timelines split (transcript vs. durable activity), write mutations + idempotency, the Work/Case read model, what each adapter translates. |
| [`SCREENS_AND_COMPONENTS.md`](SCREENS_AND_COMPONENTS.md) | Route-by-route tour + what each component group (`shell/`, `sessions/`, `timeline/`, `system/`, `work/`, `ui/`) does. |
| [`DEV_AND_BUILD.md`](DEV_AND_BUILD.md) | Running dev, testing, building, how the gateway serves the built UI, PWA/service-worker notes. |

## Related, outside this folder

| Doc | Why |
|---|---|
| [`docs/CONTROL_CONTRACT.md`](../CONTROL_CONTRACT.md) | The backend-side half of the contract this UI binds to — event envelope, inbound entry points, backend registry, read model. |
| [`docs/archive/frontend-backend-gap/FRONTEND_BACKEND_GAP.md`](../archive/frontend-backend-gap/FRONTEND_BACKEND_GAP.md) | 🟡 superseded — the historical gap analysis that produced the ✅/🟡/❌/⛔ tags you'll see in `web/src/domain/*.ts`. The in-code comments are current; this is the trace. |
| [`docs/SESSION_STATE_TIMELINE_ARCHITECTURE_REVIEW.md`](../SESSION_STATE_TIMELINE_ARCHITECTURE_REVIEW.md) | Why the durable session timeline / diagnostic-vs-durable split exists. |
| [`docs/ENV_FEATURE_FLAGS.md`](../ENV_FEATURE_FLAGS.md) | Feature flags that gate what data the Web UI actually sees live (e.g. `HARNESS_FLOW_DRIVE` for the Work tab). |
| [`docs/RUNBOOKS/OPERATIONS_PM2.md`](../RUNBOOKS/OPERATIONS_PM2.md) / [`CONTROL_SURFACE_DEPLOY_RUNBOOK.md`](../RUNBOOKS/CONTROL_SURFACE_DEPLOY_RUNBOOK.md) | Running/deploying the gateway process this UI is served from. |

## Maintenance

The code itself carries most of the detail — every `domain/`, `transport/`,
and `hooks/` file has an accurate header comment, and `domain/*.ts` types are
individually tagged with backend-parity status. **These docs are the map, not
a duplicate of that detail** — when a file's own doc-comment changes in a way
that contradicts a claim here, trust the code and fix this doc, not the
reverse. Add a new file to this folder only when a concern doesn't fit the four
above; don't split further for one section's worth of content.
