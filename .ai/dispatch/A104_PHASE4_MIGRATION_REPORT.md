# A104 Phase 4 — inbox migration evidence

Dry-run on a copy of the production DB (`sqlite3 'file:…/mesh.db?mode=ro' ".backup …"` at 2026-10-09T18:53:49Z, schema 44 → migrated to 46 on the copy by opening it):
`python scripts/a104_seed_inbox.py --db <copy> --report <scratch>/a104_dryrun` → exit 0.

## A104 Phase 4 inbox migration — DRY-RUN
- db: `/tmp/claude-1000/-home-cifran-dev-AI-team/bc87015c-db36-44a8-90f1-7eea9feb4c82/scratchpad/dryrun.db` · generated 2026-10-09T18:54:12.251996+00:00
- genuine completions: 2 ['task_b7ba302c', 'task_e7ae0733'] · **lost: 0** []
- continuation tokens discharged: 4 ['cont:312ef564a2b142d49b1f50d74a29bc9a:1', 'cont:4d8a46b52d8a48bfa965350aa9bf0a64:5', 'cont:83d10aec8e2445679eff537908f4f536:3', 'cont:c96c77855a4c4bbab95faef631df4205:1']
- never-run telemetry rows relabelled cancelled→withdrawn: 1560

| case | mode | status | pending before | pending after | seeded / revived | acked | junk retired | filters |
|---|---|---|---|---|---|---|---|---|
| 4e1895ef | legacy | open | [] | [] | [] | [] | [] | [] |
| a096b953 | legacy | open | [] | [] | [] | [] | [] | [] |
| a7516d4a | legacy | open | [] | [] | [] | [] | [] | [] |
| 989ada13 | legacy | open | [] | [] | [] | [] | [] | [] |
| af00910e | legacy | open | [] | [] | [] | [] | [] | [] |
| 8b7466c2 | legacy | open | [] | [] | [] | [] | [] | [] |
| 2982ef8d | legacy | open | [] | [] | [] | [] | [] | [] |
| cec750ec | legacy | open | [] | [] | [] | [] | [] | [] |
| 9ac80939 | legacy | open | [] | [] | [] | [] | [] | [] |
| 00c48658 | legacy | blocked | [] | [] | [] | [] | [] | [] |
| 4d8a46b5 | legacy | open | [] | ['task_b7ba302c'] | ['task_b7ba302c→f078ce07f3d1 (manager_seat_at_dispatch)'] | [] | [] | [] |
| c96c7785 | outbox | open | [] | [] | [] | [] | ['task_786e66ee'] | [] |
| 534463b6 | outbox | open | [] | ['task_e7ae0733'] | ['task_e7ae0733→0d33070dab74 (executing_member_at_dispatch)'] | [] | ['cturn_0a26d677401fb2e28d20319c'] | [] |
| 312ef564 | outbox | open | [] | [] | [] | [] | ['task_a6896384'] | [] |
| 83d10aec | outbox | blocked | [] | [] | [] | ['task_7b175284 (reviewed)'] | ['cturn_9a02de18f39c1be04b6fff9f', 'task_637e22b0', 'task_f5327548'] | [] |

Note — `4d8a46b5`: the seeded recipient `f078ce07f3d1` (Manager seat at dispatch) is closed. Delivery reaches the existing A55 dead-Manager gate, where respawn approval `appr_82d88cb8351e` (`case_manager_respawn`) has been **pending since 2026-10-07T15:13Z** — the same gate the legacy token `cont:4d8a46b5…:5` sat behind for two days. No paid respawn happens without the operator.
