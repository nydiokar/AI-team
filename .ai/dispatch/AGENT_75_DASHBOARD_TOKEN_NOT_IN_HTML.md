```yaml
job_id: AGENT_75_DASHBOARD_TOKEN_NOT_IN_HTML
created_at: "2026-08-05T09:34:34.482453+00:00"        # CANONICAL — set once at dispatch, never derive again
status: done              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md#A75             # -> DISPATCH_LOG.md section with the verdict prose
evidence: ["src/control/control_api.py","tests/test_control_api_webui.py"]                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-11-26T10:00:08.835740+00:00"
```

# DISPATCH — AGENT_75_DASHBOARD_TOKEN_NOT_IN_HTML

**Level:** 2 (defense-in-depth, larger web-touching PR) · **Type:** remove the control-API token
from the served dashboard HTML; authenticate the dashboard without exposing the secret in a
`window` global.
**Status of this packet:** ready (authored, not executed)
**Depends on:** — (P2-4 from A67; operator agreed the tailnet is the real boundary and this is a
hygiene gap, and that the clean fix is best done together with the credential work — so this job is
**sequenced after/alongside A71**, not before it).

> **Why this packet exists.** P2-4: the dashboard's served HTML embeds the control-API token in a
> `window.__DASHBOARD_TOKEN__` global (`src/control/control_api.py` ~2398 injection). Anyone who can
> read the served page (XSS, a browser extension, a local process reading the fetched HTML) gets the
> token without touching the `.env`. The tailnet binding is the real boundary (LAN bind stays off),
> but the token should not be a side-channel for free. Because the dashboard auth flow (TokenGate)
> is coupled to how the page authenticates to the API, this is a bigger web change than the other
> small jobs — sequence it deliberately.

## Task

1. Design (short note first): serve the dashboard HTML with the token **not** embedded as a
   `window` global. Options to evaluate against the existing TokenGate/auth flow:
   - short-lived, per-session dashboard nonce issued over the same authenticated bootstrap and
     exchanged for API auth (cookie or header), or
   - read the token from `localStorage`/a cookie the user supplies once, never server-injected, or
   - split the token out to a dedicated `/api/config/token` endpoint guarded by the same bound.
   Pick the option that keeps the existing UX and auth boundaries intact and is flag-gated if it
   changes behavior.
2. Implement web-side + API-side change; verify the TokenGate flow still works (dashboard loads,
   dispatch/control calls authorized).
3. Tests (plain `pytest`, touched modules only): served HTML contains no token literal; dashboard
   bootstrap still yields a working authenticated session.
4. Docs: update `docs/backend/MESH_SECURITY.md` storage/trust notes to record the token is no longer in
   served HTML.

## Constraints / hard rules

- Branch policy: `feat/<slug>` + PR + self-merge; non-disclosing PR description.
- `pytest` on touched modules only; never the full/e2e suite.
- Gateway-side only: restarting the gateway after merge is fine; never touch the worker.
- Keep LAN bind off; the tailnet remains the stated boundary — this removes the side-channel, it
  does not add a new auth layer.
- Do not carry other loops' uncommitted edits into your merge.
- This job may be picked up only after A71 has landed its design (the operator wanted the
  credential work and this fix to move together).

## Done when

- Served dashboard HTML no longer contains the token literal; TokenGate flow verified green.
- Targeted `pytest` evidence recorded; PR merged to `main`; gateway restarted and `/health` ok.
- Set `evidence:` to the test report + PR ref and `status: done`.

## Closure — 2026-11-26 (registry reconciliation, superseded wording)

**Verdict: done, with the literal Done-when wording explicitly superseded.** PR
[#150](https://github.com/nydiokar/AI-team/pull/150) (merge `db6280b`) removed the unconditional
`window.__DASHBOARD_TOKEN__` global — `test_index_never_embeds_token` and the SPA fallback test
assert no token literal for ordinary/local requests. PR
[#156](https://github.com/nydiokar/AI-team/pull/156) (merge `c448400`) then **deliberately
re-introduced injection for trusted remote tailnet peers only**, gated by `_ui_request_trusted`
(`src/control/control_api.py:868`): Host must be the bind host, a `*.ts.net` MagicDNS name or a
tailnet IP literal, *and* the peer must be a remote tailnet device (loopback/this-host peers are
refused; DNS-rebinding names are refused). Those requests get `no-store`, and
`test_untrusted_request_never_gets_token` covers the refusal cases.

Therefore the original absolute claim ("no token in any served HTML") is **no longer literally
true** and is not claimed here. What holds: the page-inspectable side-channel for untrusted
requests is closed, TokenGate still works (trusted remote injection + header auth), and no new
auth layer was added. This is a later main-branch design decision, not a silent weakening of this
job; record it as such at review. Targeted verification 2026-11-26:
`pytest tests/test_control_api_webui.py …` → 121 passed, 2 skipped across the six closure suites.
No new live gateway probe was run from this dev host.
