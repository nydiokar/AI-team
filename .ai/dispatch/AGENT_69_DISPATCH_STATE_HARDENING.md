```yaml
job_id: AGENT_69_DISPATCH_STATE_HARDENING
created_at: "2026-08-04T04:17:26+03:00"        # CANONICAL — set once at dispatch, never derive again
status: done              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: ["scripts/dispatch/dispatch_state.py", "tests/test_dispatch_state.py", "pyproject.toml"]                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-04T01:18:27.272434+00:00"
```

# DISPATCH — A69 · Dispatch state hardening reconstruction

**Level:** 2 · **Type:** tooling + regression tests

## Goal

Reconstruct the useful, unfinished parts of the discarded dispatch-kit experiment without restoring
unsafe automation: completed packets must name real proof, auditing must tolerate legacy naive
timestamps, and dependency completion must never silently change an operator-gated packet.

## Scope

- Flag `done` packets with missing or empty evidence.
- Normalize naive ISO timestamps as UTC before age calculation.
- Add an explicit `auto_unblock: true` per-packet opt-in and a manual `--resolve-blocks` command.
  Dependency completion alone is never consent; packets such as A57 remain blocked until their
  operator gate is removed deliberately.
- Declare the optional parquet dependencies and add regression tests/self-test coverage.

## Done when

- Focused pytest and the script self-test pass.
- `--audit --strict` reports actionable state defects with a nonzero exit status, while ordinary
  audit remains informational.
- Generated dispatch views are refreshed, and this packet records closure evidence.

## Closure

Implemented and verified on 2026-08-04. The audit now treats every `done` state as a claim that
must cite an existing artifact; legacy timestamps without an offset are normalized as UTC; and
dependency completion is advisory unless the dependent packet declares `auto_unblock: true`.
Even then, `--resolve-blocks` is an explicit operator command. Focused regression tests, the
script self-test, and strict audit all pass; the resolver reported no eligible packets.
