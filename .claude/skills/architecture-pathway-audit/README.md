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

For AI-Team inbox ownership, use `contracts/agent_inbox.yaml` with
`tests/test_a104_single_pending_source.py`. The contract checks store access.
The repository test checks that wake production and activation consult
`pending_for` and agree on message state.

Exit statuses: **0** completed applicable checks with no unwaived violations;
**1** completed checks with violations; **2** invalid or incomplete required
analysis. Required scope paths that resolve to no scanned files, skipped input
and Python parse errors prevent a passing verdict. SQL access facts retain the
execution site and declaration where available. Schema-qualified and quoted
identifiers are recognized, CTE references respect statement scope and SQL
examples in docstrings do not create accesses. Unsupported dynamic SQL remains
visible as a coverage limitation. Comments are ignored. Tests and historical
references may require explicit, reasoned waivers.

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
| `--refresh` | Reindex and drop cached expansions |
| `--no-open`, `--port`, `--no-serve` | Browser launch, local port, or write artifacts and exit |
| `--baseline` | Reviewed acknowledgements; default `.arch-audit/baseline.json` |
| `--baseline-update` | Acknowledge the emitted findings on a completed sweep after review |
| `--findings-offset` | Return the next evidence page using the previous report’s `next_findings_offset` |

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

Read the JSON report’s `run`, `coverage` and `analysis_scope` fields together.
The run identifies the current attempt, source digest, effective scope, analysis
stage and errors. A batch run returns **2** when required analysis is incomplete
or failed. A completed execution and a deliberate partial search can each
return **0**. Inspect `run.status` to tell them apart. A completed zero means
only that the declared static checks emitted no matching findings on the
specified input. The legacy report format lacked this verdict.

The report includes the number of findings before and after per-kind output
bounds and the continuation offset for omitted evidence. To retrieve additional
evidence, repeat the command against the same source and options, with
`--findings-offset` set to the previous report’s `next_findings_offset`. Match
source digest and settings before comparing pages. Known/stale baseline metadata
and mechanical facts remain distinct from candidate findings. Semantic defects
require source and behavioral review.

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
and competing-path detection. It skips when CBM is missing.

Run and score the bundled corpus using the verified CBM release and a disposable
results directory. The runner records one manifest per invocation and returns
nonzero if any required analysis fails or omits findings.

```bash
audit_dir=.claude/skills/architecture-pathway-audit/benchmark
audit_out=$(mktemp -d)
.venv/bin/python "$audit_dir/run_corpus.py" "$audit_dir/corpus" "$audit_out"
.venv/bin/python "$audit_dir/score.py" "$audit_dir/corpus" "$audit_out"
```

The exact source labels identify candidate localization independently from
verified contract violations. Structural facts never count as localization.
Benign controls have their own flagged and unknown counts. See
`tests/test_benchmark.py` for independent SQLite replay of the ledger witnesses
and [MEASUREMENTS.md](MEASUREMENTS.md) for results.
