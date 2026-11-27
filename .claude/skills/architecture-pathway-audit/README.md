# Architecture Pathway Audit

Inspect execution paths and check whether an architectural cutover has reached its
declared end state. Two tools serve different purposes:

| Tool | Purpose | Output |
|---|---|---|
| `scripts/contract_check.py` | Retired symbols, layer boundaries, SQL store ownership at a git revision | File/line violations and exit status |
| `scripts/pathway_explorer.py` | FastAPI routes, calls, shared SQL tables and competing implementations | Browser explorer, HTML snapshot and JSON report |

The explorer produces structural facts and candidates. Confirm defects by reading
the implicated implementations, callers, configuration and consumers. Neither tool
proves runtime correctness or deployment state.

## Requirements

Use the repository's configured Python version and development environment
(`pyproject.toml`; AI-Team requires Python 3.12+). The contract checker uses
Pydantic 2 and PyYAML. Follow the project's dependency installation workflow.

The explorer also requires `codebase-memory-mcp`. Supply `--cbm /absolute/path`,
set `CBM_BIN`, or put it on `PATH`; Linux also checks
`~/.local/bin/codebase-memory-mcp`. Obtain the binary from the
[upstream releases](https://github.com/DeusData/codebase-memory-mcp/releases) and
verify its published checksum. Tested versions and measurements live in
[MEASUREMENTS.md](MEASUREMENTS.md).

## Check a declared end state

Run from the repository root:

```bash
.venv/bin/python .claude/skills/architecture-pathway-audit/scripts/contract_check.py --contract .ai/dispatch/AGENT_102_UNIFY_BACKEND_PATHWAYS.md --rev HEAD --json /tmp/architecture-contract.json
```

`--contract` accepts YAML or a Markdown packet containing exactly one
`arch-contract` fenced YAML block. `--repo` selects the repository. Omit `--rev`
to inspect working files, including untracked files that are not ignored.

```yaml
scope: [src, scripts]
retired: [old_send]
retired_patterns: ['^run_legacy_']
forbidden_in:
  src/backends: ['^timeout_seconds$']
stores:
  agent_inbox:
    writers: [src/control/agent_inbox.py]
waivers:
  - match: 'retired:old_send'
    path: scripts/migrate_legacy.py
    reason: 'Reads the old format for the documented one-time migration.'
```

Scope entries select git paths; `exclude` skips path prefixes/globs. Store rules
accept file globs or `path::qualified_function` globs. An omitted reader/writer
constraint permits that access; an empty list permits nobody. Choose constraints
around actual responsibilities: lifecycle queries are not necessarily competing
pending-state read models.

Exit statuses: **0** no unwaived violations found; **1** violations; **2** invalid
contract/input. Always inspect coverage: zero scanned files, skipped files and
Python parse failures can invalidate a passing result. SQL evidence identifies
static execution sites, including supported schema-qualified and quoted table
names, scoped CTEs, and shared string declarations. Docstring SQL does not
generate store accesses. Contract JSON reports the count and up to 100 examples
of unresolved SQL analysis notices. Reported accesses are static evidence.
Python symbol mentions in docstrings are notes. Comments are ignored.
Tests and historical references may require
explicit, reasoned waivers.

In AI-Team, `scripts/dispatch/dispatch_state.py --set <job> status done` runs the
checker against `HEAD` when the packet contains an `arch-contract` block. This is
an opt-in check of declared assertions. Frozen examples in `benchmark/history/`
preserve historical intent and need not match a maintained packet's later waivers.

## Explore execution paths

```bash
.venv/bin/python .claude/skills/architecture-pathway-audit/scripts/pathway_explorer.py --repo . --scope src --project ai-team-local-audit
```

Indexes only `src/`, serves `http://127.0.0.1:8766/` and opens a browser. In the 2D
explorer, select a route, expand downstream/upstream and inspect Coverage alongside
the evidence. Ctrl+C stops the explorer; check CBM's separate daemon lifecycle
after a disposable run.

For a bounded batch sweep without a server:

```bash
.venv/bin/python .claude/skills/architecture-pathway-audit/scripts/pathway_explorer.py --repo . --scope src --project ai-team-local-audit --no-serve --sweep-depth 6 --sweep-cap 600 --roots all --out .arch-audit/pathways.html --report .arch-audit/findings.json
```

| Option | Purpose |
|---|---|
| `--scope` | Existing folder inside `--repo`; `.` selects the repository |
| `--roots routes` / `--roots all` | Routes only, or routes plus unreferenced public callables; private entry points are not all-root seeds |
| `--sweep-depth`, `--sweep-cap` | Bound recursive exploration per root |
| `--neighbors`, `--timeout` | CBM page size and query timeout |
| `--cache-dir` | Explicit cache; Linux default is under `~/.cache/architecture-pathway-audit/` |
| `--refresh` | Reindex and drop expansions; use a new cache directory when changing Python versions |
| `--no-open`, `--port`, `--no-serve` | Browser launch, local port, or write artifacts and exit |
| `--baseline` | Reviewed acknowledgements; default `.arch-audit/baseline.json` |
| `--baseline-update` | Acknowledge **every** current finding and exit; use only after reviewing that exact set |

The scope suffix is appended to the CBM project name. Content changes trigger
reindexing and invalidate affected expansions. The Python AST adapter resolves
router prefixes, factories and `include_router` chains, supplementing CBM's graph
with nested-function calls and SQL accesses. Findings retain evidence provenance.

Rules cover convergence and diverge/rejoin facts; bypass, configuration divergence,
multiple writers, package cycles, similar/sibling implementations, behavioral twins
and matching footprints; orphan SQL media, truncated windows and split readers.
Similar structure can be intentional. Baselines mark findings `known` without
hiding them; vanished IDs appear as `stale_ids`.

## Interpret coverage before findings

Read `coverage` and `analysis_scope` in JSON: route discovery/expansion counts,
parse failures, unresolved calls, incomplete pages, hub boundaries and finding
caps. Graph rules use loaded neighborhoods; SQL/fingerprint rules also inspect
AST definitions outside expanded routes. Bounded sweeps are not complete audits.
Every report now carries a `run` object: `status` (`complete` / `incomplete` /
`failed`), `required_analysis_complete`, typed `errors`, capability gaps, source
and identity digests, and cache invalidation reason. It is shared by JSON, the
browser banner/coverage pane and the `--no-serve` CLI exit code (0 complete, 2
incomplete/failed). Findings count never drives that exit code: a bounded
zero-finding scan is clean only when required analysis actually completed.
Artifacts are published atomically, so a failed run cannot leave an old clean
report looking fresh. Candidate counts are still structural leads, never
semantic verdicts. `contract_check.py` follows the same rule: exit 0 only over
applicable completed checks, 1 for completed checks with violations, 2 for a
wrong/empty scope, skipped/binary/oversize required input or unparseable Python.

Dynamic dispatch, unknown receiver types, callbacks, conditional registration and
sub-app mounts limit coverage. Scope may exclude scripts, config, frontend code
or another process that changes behavior. A call graph cannot prove statement
order, authorization, transactions or failure recovery.

## Verify the tool

Run only the skill's tests; they use disposable fixtures and do not invoke a paid
coding-agent CLI:

```bash
.venv/bin/python -m pytest .claude/skills/architecture-pathway-audit/tests --tb=short
```

`test_real_cbm.py` exercises the real binary, expansion, cache reuse/invalidation
and competing-path detection. It skips when CBM is missing; a skip does not prove
compatibility. The benchmark runner/scorer require an external corpus containing
`base/`, `variants/`, `controls/` and `GROUND_TRUTH.json`. The scorer's `CAUGHT`
label measures entity touches, not verified defect recall. Results, reviewed
findings, reproductions and experience belong in [MEASUREMENTS.md](MEASUREMENTS.md).
