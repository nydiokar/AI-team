---
name: architecture-pathway-audit
description: Inspect mechanically derived execution routes using a bounded, scoped Codebase Memory graph explorer; evaluate only evidence-backed structural anomalies.
---

Run `scripts/pathway_explorer.py --repo . --scope src --project <project-name>`; the script owns indexing, caching, route queries, and graph visualization. Do not have an agent invent its own graph edges or assume a known defect in its query design. Analyze source-file-scope boundaries and report the explicit coverage status. Use the 2D browser explorer to progressively inspect paths and produce source-backed findings; classify suspected competing implementations only after comparing the source implementations and configuration contracts. Do not claim a clean system from incomplete graph coverage. Do not modify application code without authorization.
