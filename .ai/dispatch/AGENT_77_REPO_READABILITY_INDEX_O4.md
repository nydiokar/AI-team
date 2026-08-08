```yaml
job_id: AGENT_77_REPO_READABILITY_INDEX_O4
created_at: "2026-08-08T23:19:02.923650+00:00"        # CANONICAL — set once at dispatch, never derive again
status: active              # ready | active | blocked | done | dead
owner: worker:22e50fd3bdac
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-08T23:21:28.528038+00:00"
```

# DISPATCH — AGENT_77_REPO_READABILITY_INDEX_O4

**Level:** 2 (research/measurement spike — ships NO gateway-resident service) ·
**Type:** research/diagnosis + minimal proof-of-concept.
**Status of this packet:** ready (authored, not executed)
**Depends on:** — (M3 open question **O4**, `docs/M3_MANAGER_INVOCATION_SPEC.md` §7; operator
green-lit the O1/O4 token-lane on Case `67b0ec8c…`).

> **Why this packet exists (O4).** The biggest token sink in this harness is **orientation
> reads** — agents read whole files to find a symbol. A repo-map / symbol-graph (universal-ctags,
> tree-sitter tags, or `codebase-memory-mcp`, v0.6 §"Optional accelerator") lets an agent resolve
> `symbol → file:line` from an index and read only the relevant span, cutting per-session tokens
> directly. **HARD SPEC CONSTRAINT (O4, verbatim): "Off-box trial first (ARM64 + RSS vs the Pi's
> memory pressure) before it touches the gateway host."** The gateway runs on a memory-constrained
> Raspberry Pi (ARM64). This job therefore **measures and recommends — it does NOT install any
> resident indexer/MCP on the gateway host.** Any gateway-resident install is a separate,
> operator-gated follow-up decided on this job's evidence.

## Task (research + measure + minimal PoC — NO gateway service)

1. **Ground first.** Read `docs/M3_MANAGER_INVOCATION_SPEC.md` §7 (O4), the `codebase-memory-mcp`
   accelerator note in `docs/Task_Harness_v0.7_AUTOMATION.md` (~line 226) and its v0.6 origin, so
   the recommendation is anchored to what the spec already contemplated.
2. **Evaluate candidates** for a repo symbol index on THIS repo: at minimum **universal-ctags**
   and **tree-sitter tags**; assess `codebase-memory-mcp` on footprint/maintenance/fit (do not
   install a heavy MCP on the Pi to test it — reason from its resource profile + a contained trial
   if trivially available). For each: how an agent queries `symbol → file:line`, index build cost,
   on-disk index size, and **resident RSS** if any daemon is required.
3. **Measure honestly** on this repo (`src/`, `scripts/`, `web/`): build the ctags/tree-sitter
   index, record wall-time, index size, and peak RSS of the build (`/usr/bin/time -v` or
   `resource`-based). State the numbers — no estimates presented as measurements. If a candidate
   needs a resident daemon, characterize its idle RSS from its own docs and flag it against the
   Pi's memory headroom (report current free memory as context).
4. **Decide the deployment shape**: per-project vs gateway-wide, and **on-demand CLI invocation
   (no resident process) vs a resident indexer**. Given the Pi constraint, explicitly evaluate the
   *stateless* option: agents shell `ctags`/`readtags` (or a tree-sitter query) on demand and read
   only the resolved span — zero resident RSS. Recommend one, with the token-saving argument
   (orientation-reads avoided) and the memory cost.
5. **Minimal PoC** (repo-local, no service): a small script (e.g. `scripts/repo_index/symbol_lookup.py`
   or a documented `ctags`+`readtags` recipe) that, given a symbol name, returns `file:line` from a
   generated tags index over this repo — proving an agent can orient via the index instead of
   reading whole files. This is a tool an agent *invokes*, not a daemon the gateway hosts.

## Constraints / hard rules

- **Ship NO gateway-resident indexer/MCP.** Off-box/stateless first is the spec mandate; a resident
  install on the Pi is an operator-gated follow-up, not this job.
- `pytest`/plain scripts on touched modules only — never the full/e2e suite (paid CLI cost guard).
- Do not `pip install` heavyweight packages into the gateway venv as the plan; prefer OS `ctags`
  or a contained trial. If a dependency is genuinely needed for the PoC, add it to `pyproject.toml`
  and justify it — do not leave ad-hoc installs.
- Branch: docs + a small self-contained script ⇒ `feat/<slug>` + PR + self-merge (the PoC touches
  `scripts/`); if you end up docs-only, `main` is fine per policy. Minimal diff.
- Convert relative dates to absolute; report measurements with the command that produced them.

## Done when

- `docs/REPO_READABILITY_O4.md` exists: candidate comparison, **measured** build-time/index-size/RSS
  numbers with the commands used, the per-project-vs-gateway + stateless-vs-resident recommendation
  with the Pi memory-pressure verdict, and the token-saving argument.
- A working **repo-local** PoC that resolves `symbol → file:line` from an index over this repo, with
  a recorded example run (input symbol → output location).
- NO gateway-resident service installed; any such install is explicitly deferred to the operator.
- PR opened **and merged to `main`** by you (or committed to `main` if docs-only); no dangling branch.
- Set `evidence:` to the doc + PoC script + example-run output, `results_ref:` to the DISPATCH_LOG
  row, and `status: done`.
