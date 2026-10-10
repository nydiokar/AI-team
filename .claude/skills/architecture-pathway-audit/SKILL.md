---
name: architecture-pathway-audit
description: Inspect mechanically derived execution routes using a bounded, scoped Codebase Memory graph explorer; evaluate only evidence-backed structural anomalies. Also checks a job's declared end state (arch-contract) against a git revision — use it to verify a cutover/unification/retirement is actually complete ("is the old path really gone?").
---

**Completion check (primary use).** `python scripts/contract_check.py --contract <packet.md|contract.yaml> --rev <REV>`
verifies a declared end state deterministically: retired names gone everywhere in scope (defs, references,
imports, string dispatch, prompts), layering bans (`forbidden_in`), store ownership (`stores`). Exit 0 met,
1 violations (file:line listed), 2 bad contract. `dispatch_state.py --set <job> status done` runs it
automatically for packets with an ```` ```arch-contract ```` block. Examples: `benchmark/history/*.yaml`.

Run `scripts/pathway_explorer.py --repo . --scope src --project <project-name>`; the script owns indexing, caching, route queries, and graph visualization. Do not have an agent invent its own graph edges or assume a known defect in its query design. Analyze source-file-scope boundaries and report the explicit coverage status. Use the 2D browser explorer to progressively inspect paths and produce source-backed findings; classify suspected competing implementations only after comparing the source implementations and configuration contracts. Do not claim a clean system from incomplete graph coverage. Do not modify application code without authorization.
