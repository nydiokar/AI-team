# Architecture Pathway Explorer — src-scoped, CBM + FastAPI AST adapter

## Run (PowerShell)

```powershell
python .claude/skills/architecture-pathway-audit/scripts/pathway_explorer.py --repo . --scope src --project ai-team-local-audit --out .arch-audit/pathways.html --report .arch-audit/findings.json
```

Serves `http://127.0.0.1:8766/` and opens the browser. `--no-serve` writes HTML/JSON and exits;
`--no-open`, `--port`, `--refresh`, `--sweep-depth N` (pre-expand every route N levels for repo-wide findings),
`--roots all` (also sweep every unreferenced public callable: worker loops and interface-dispatch targets that no HTTP route reaches).

## How it works

- **One long-lived CBM MCP stdio session** (`cbm_client.py`, verified on codebase-memory-mcp 0.11.0). Warm
  queries take ~20-40 ms; the CLI form pays ~2.5 s per call. CBM tool errors raise `CBMError` and are never cached
  as an empty neighbourhood. CBM's Cypher subset rejects `id()`/`labels()` in WHERE, so nodes are looked up by
  `qualified_name`.
- **Scope:** `--scope src` indexes only that folder into CBM project `<project>-src`. The index persists in CBM; it
  is re-run only when a source file's content hash changed.
- **Routes:** a generic FastAPI AST adapter (`pyast_adapter.py`) resolves decorators, `APIRouter` prefixes and
  `include_router` chains, and links handlers to CBM symbols by file+line (CBM misses nested handlers and prefixes).
  The full inventory is loaded at startup (no CBM query); it is cross-checked against CBM Route nodes in the
  Coverage tab.
- **Expansion:** on demand, per (node, direction), cached in
  `%LOCALAPPDATA%/architecture-pathway-audit/<hash>/pathway_cache.json`; only entries touching changed files (and
  all `in` entries) are invalidated.
- **Findings** (`structure.py`) are deterministic graph rules (fact -> candidate); semantic verification is a separate,
  agent-assisted step.

## Measured (this repo, `src/`, 130 routes, 3077 CBM nodes / 13.7k edges, 2026-10-09)

| Case | Time |
|---|---|
| Cold: fresh CBM index + `--sweep-depth 4` over all 130 routes (1216 CBM calls) | ~96 s (index 12.8 s, sweep 79 s) |
| Warm, unchanged source, full cached sweep (1 CBM call) | ~3.5-4 s |
| Server ready (routes listed) | ~0.1 s; CBM stats warm in background ~1 s |
| Repeat expansion | local cache hit, 0 CBM calls |

## Rules (structure.py)

Facts: convergence, diverge/rejoin. Candidates: bypass of a common gate, overlapping route handlers,
`SIBLING_OVERLAP` (operations of one class sharing most downstream components, neither calling the other;
clustered per class), duplicate implementation (CBM SIMILAR_TO), config divergence, multiple writers, package cycles.
Shared-medium layer (SQL tables from string literals, AST-derived): `ORPHAN_MEDIUM` (written never read / read never
written), `TRUNCATED_WINDOW` (oldest-N reads of an insert-only table), `SPLIT_READERS` (tables written together by a
focused operation but read by disjoint focused readers). `Hubs` (high fan-in nodes) are listed in `analysis_scope`
instead of being silently excluded. Table access via helper constants or ORMs is not seen.
All are candidates until a human/agent verifies semantics; legitimate variants over a shared core are flagged too.

## Coverage and limitations

Progressive explorer, not a comprehensive audit: findings are computed only over expanded neighbourhoods
(`analysis_scope` in the report). Dynamic calls, ambiguous method names and unresolved callbacks are listed per node
and in the Coverage tab; absence of an edge is not proof of absence. Sub-app mounts (e.g. `StaticFiles`) and
conditionally registered routes are reported as route limitations. Python only for route discovery.

## Tests

```powershell
python -m unittest discover -s .claude/skills/architecture-pathway-audit/tests -t .claude/skills/architecture-pathway-audit/tests
```

Includes `test_real_cbm.py`, which drives the real CBM binary against a disposable fixture (cache reuse,
invalidation, competing-path detection). It never touches the paid Claude CLI.
