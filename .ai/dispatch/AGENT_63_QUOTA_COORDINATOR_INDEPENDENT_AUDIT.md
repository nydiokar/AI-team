```yaml
job_id: AGENT_63_QUOTA_COORDINATOR_INDEPENDENT_AUDIT
created_at: "2026-08-01T13:48:33+03:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-03T13:20:23.195320+00:00"
```

# DISPATCH — A63 · Quota coordinator: independent audit of the direct-to-main finalization commit

**Level:** 2 (read-mostly audit; code changes only if the audit finds a real gap, no paid provider
calls beyond what the existing test fixtures already do) · **Type:** audit, code-if-needed
**Authored:** 2026-08-01 · **Status of this packet:** ready (authored, not executed)
**Depends on:** — (audits work already on `main`)

> **Read this first — why this packet exists.** `A61` (`AGENT_61_QUOTA_COORDINATOR_FINALIZATION.md`)
> was dispatched to turn the salvaged-but-placeholder quota coordinator (PR #47, `Unsupported`
> Claude adapter) into a real observer. Its packet's own `## Closure` section is filled in — all six
> milestone boxes checked, evidence pasted — but that Closure was **self-reported and never
> independently reviewed**: the corresponding code landed as commit `cbbaa10` ("Finalize quota
> coordinator wiring"), committed **directly to `main`** by the operator (`nydiokar`), with **no
> `feat/<slug>` branch and no PR** — i.e. it skipped this project's own branch+PR+review policy
> (`CLAUDE.md`: "Any `src/`/config/migration change cuts one `feat/<slug>` branch and opens a PR").
> The operator explicitly asked (2026-08-01) for someone to verify the merged implementation isn't
> contradicting what was actually planned, and to make sure what's real is what's live — this job is
> that verification, done from outside, against the spec and the code, not against the packet's own
> claims.
>
> **Also resolve, don't assume:** the operator raised the possibility that "the real" implementation
> might live somewhere this repo can't see (a different clone/machine). Before trusting `main` as
> authoritative, the operator has been asked directly whether such a location exists. If yes, this
> job's actual target may need to change — check with the operator/dispatcher before deep-diving if
> that question is still open when this job is picked up.

## Why (intent)
`cbbaa10` is a large diff (24 files, ~1000 lines) that bypassed review and claims to finish the
quota coordinator per spec. Trusting a self-reported Closure section on unreviewed code — especially
code that will eventually run against a live, paid, shared Claude account — is exactly the failure
mode this project's review gate exists to catch. This job re-runs that gate after the fact.

## TASK
1. **Re-walk A61's own gap table** (`AGENT_61_QUOTA_COORDINATOR_FINALIZATION.md`, the "Spec-vs-reality
   gap" section) against the CURRENT tree, line by line — do not trust the Closure section's
   "Evidence" bullets at face value; re-derive each one independently:
   - Is `ClaudeStatusLineQuotaAdapter` (`src/services/quota_window_coordinator.py:553`) actually
     wired into `build_default_quota_adapters()` (confirmed present at a glance — verify it's
     correctly used, not just present)?
   - Does it genuinely spend **0 Claude tokens** — trace `scripts/claude_statusline_capture.py` end
     to end and confirm it never invokes `claude`/the SDK/an API call, only reads captured
     status-line JSON.
   - Is `principal_hash` derivation correct per spec §9A's priority order, not just present?
   - Does `GET /api/quota-windows` (`src/control/control_api.py`) match the Phase-1 doc's claimed
     response shape, and is it actually bearer-protected?
   - Is the adaptive-cadence back-off (spec §17 step, operator's "don't spam providers" complaint)
     really implemented, not just claimed — find the code path, not just the test that exercises it.
   - Confirm the Telegram digest subscriber (`src/services/quota_digest.py`) is genuinely a separate
     module that the coordinator itself never imports/calls (spec §15 boundary).
2. **Compare against the two spec docs directly** (`docs/SESSION_WINDOW_WARMING_SPEC.md`,
   `docs/QUOTA_WINDOW_COORDINATOR_PHASE1.md`, both touched by `cbbaa10`) — check whether the docs
   were updated to match the code or whether either still asserts something the code doesn't do
   (the original A61 dispatch flagged the two docs as self-contradictory; confirm that's now
   resolved to ONE truth, not just moved).
3. **Confirm byte-identical-when-OFF still holds** — `QUOTA_COORDINATOR_ENABLED=false` (its current
   default/registry state — check `scripts/ops_flag.sh get QUOTA_COORDINATOR_ENABLED` for the live
   value first) must mean zero DB file, zero observe loop, zero digest subscriber. Re-verify, don't
   assume the earlier claim still holds after further commits.
4. **Run the targeted test suite** (`tests/test_quota_window_coordinator.py`,
   `tests/test_control_api.py -k quota`) and confirm it actually exercises the claims above, not just
   that it's green — a green suite with a fabricated-shape test (the exact failure mode A53's own
   review round caught elsewhere in this project) proves nothing.
5. **Check web UI wiring** — `web/src/components/system/QuotaWindowPanel.tsx` (added in `cbbaa10`):
   does it actually call `GET /api/quota-windows` through the typed transport, and does it render
   correctly for the disabled/no-data/unavailable/observed states it claims to handle?
6. **If the audit finds a real gap** (code doesn't match spec, or spec doesn't match code, or a claim
   in A61's Closure is unsubstantiated) — fix it as a normal `feat/<slug>` branch + PR, per policy
   (do not repeat the direct-to-main pattern this job exists to catch). If everything checks out,
   the deliverable is the written verdict itself, plus fixing DISPATCH_LOG's A61 row if it's still
   inaccurate.
7. **Do NOT activate anything.** `QUOTA_COORDINATOR_ENABLED` flag-on + gateway restart against the
   live paid account remains the operator's call (per A61's own REALITY CONSTRAINTS) — this job
   verifies the code is honest, it does not turn it on.

## TYPE
audit, code-if-needed. If code changes are required: branch `feat/quota-coordinator-audit-fixes`,
PR at close, merge per branch policy. If the audit finds nothing wrong: no branch needed, just the
written verdict + a DISPATCH_LOG correction if warranted.

## CONTEXT (reuse verbatim)
- The commit under audit: `git show cbbaa10` (or `git log -p cbbaa10 -- <path>` per file).
- Spec: `docs/SESSION_WINDOW_WARMING_SPEC.md`, `docs/QUOTA_WINDOW_COORDINATOR_PHASE1.md`.
- Original finalization packet (claims to verify): `.ai/dispatch/AGENT_61_QUOTA_COORDINATOR_FINALIZATION.md`.
- Code: `src/services/quota_window_coordinator.py` (`ClaudeStatusLineQuotaAdapter` ~L553,
  `build_default_quota_adapters` ~L1003), `scripts/claude_statusline_capture.py`,
  `src/services/quota_digest.py`, `src/control/control_api.py` (`/api/quota-windows`),
  `config/settings.py` (`QuotaConfig`), `src/orchestrator.py` (lifecycle wiring),
  `web/src/components/system/QuotaWindowPanel.tsx`.
- Runtime flag registry (new this session, use it): `scripts/ops_flag.sh get QUOTA_COORDINATOR_ENABLED`
  for the live current value/source; `scripts/ops_flag.sh explain QUOTA_COORDINATOR_ENABLED` for its
  description.
- Prior history for orientation only (superseded, not the audit target): the old
  `origin/phase1-quota-window-coordinator` branch and PR #47 (`feat/quota-coordinator-salvage`) —
  confirmed in this session to be genuinely superseded by `main`, not a hidden "real" version.

## ACCEPTANCE (proof, not vibes)
1. Written verdict, per gap-table row (re-derived from the tree, not copied from A61's Closure):
   confirmed-correct / confirmed-wrong-and-fixed / confirmed-wrong-and-documented-as-a-known-gap.
2. 0-token proof independently re-traced (show the actual call path, not just cite the prior claim).
3. `docs/SESSION_WINDOW_WARMING_SPEC.md` and `docs/QUOTA_WINDOW_COORDINATOR_PHASE1.md` agree with each
   other and with the code on one thing: does `GET /api/quota-windows` exist and where.
4. Flags-off byte-identical re-verified live (not just by reading code — actually check no DB file
   gets created with the flag off).
5. Targeted `pytest tests/test_quota_window_coordinator.py tests/test_control_api.py -k quota -q`
   green, and the audit states whether these tests would actually catch a regression in each claim
   above (not just that they pass today).
6. `DISPATCH_LOG.md`'s A61 row corrected if this audit finds its status line still misrepresents
   reality.

## RESERVED DECISIONS (surface, do not guess)
- **R1 — if the audit finds the code is actually fine.** Don't manufacture work to justify the job;
  a clean verdict with re-derived evidence is a complete, valuable deliverable on its own.
- **R2 — if a real gap is found that requires touching the same files `cbbaa10` touched.** Normal
  branch+PR flow: don't repeat the direct-to-main shortcut, even under time pressure.
- **R3 — external "real implementation" question.** If the operator confirms a separate
  location holds a more-complete version, get its contents (diff/patch/description) from the
  operator before starting the code-comparison steps above — don't proceed on the `main`-is-authoritative
  assumption if that's been overridden.

## SCOPE OUT
Activating the coordinator against the live account. Classification/warming (spec §7/§11/§12/§13 —
explicitly out per A61 too). Building anything not already claimed by A61's scope.

## TRAIL / EVIDENCE (fill at close)
- Re-derived gap-table verdict · 0-token trace · doc-agreement confirmation · flags-off live
  re-verification · targeted pytest result + honest note on what the tests do/don't cover ·
  web UI wiring confirmation · DISPATCH_LOG correction if needed.

---
## Milestone (burndown)
- [ ] Operator confirmed: is `main` authoritative, or is there an external "real" implementation to
      diff against first? (blocking — resolve before deep audit work)
- [ ] Gap table re-derived from the tree, each row independently verified
- [ ] 0-token spend path re-traced end to end
- [ ] Doc agreement (spec vs Phase-1 doc vs code) confirmed as ONE truth
- [ ] Flags-off byte-identical re-verified live
- [ ] Targeted tests run + honest coverage assessment written
- [ ] Web UI wiring confirmed
- [ ] Written verdict delivered; DISPATCH_LOG A61 row corrected if still wrong; any real gap fixed
      via normal branch+PR (not direct-to-main)

## Closure (fill on completion)
(fill when executed)
