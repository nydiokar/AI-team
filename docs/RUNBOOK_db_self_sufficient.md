# Runbook — make mesh.db self-sufficient & drop artifact files

End-to-end procedure to migrate the conversation + artifact data into `mesh.db` and
stop depending on `results/task_*.json`. Reversible until the final delete step.

Measured on dev (948 artifacts, 300 MB): **backfill ~5s, zero data loss**.

---

## 0. Pre-flight (on the server)

```bash
cd /path/to/AI-team
git pull                                  # get migration 17 + the new read/write paths
# Back up the DB (cheap insurance; WAL-safe copy)
sqlite3 state/mesh.db ".backup state/mesh.db.bak-$(date +%Y%m%d)"
```

The code change is backward compatible: until the backfill runs, `transcript` and
`/api/artifacts` fall back to files automatically. You can deploy the code first,
backfill later.

## 1. Apply the schema migration

Migration 17 (adds the artifact-complete columns to `mesh_tasks`) runs automatically
on first `MeshDB` init — i.e. on gateway start, or explicitly:

```bash
python -c "from src.control.db import MeshDB; MeshDB('state/mesh.db'); print('schema ok')"
sqlite3 state/mesh.db "SELECT MAX(version) FROM schema_version;"   # -> 17
```

## 2. Backfill historical turns (one-time, idempotent, ~seconds)

```bash
python scripts/backfill_conversation_turns.py        # backfill + parity check
```

Expected tail:

```
backfill done: enriched=<N> skipped_no_db_row=<x> skipped_no_text=<y> total_files=<T>
parity: checked=<C> mismatches=0 -> PASS
```

`skipped_no_db_row` = artifacts with no matching mesh_tasks row (orphans, fine).
`skipped_no_text` = genuinely empty turns (failed/diff-only, fine).
Re-running is safe (COALESCE upsert). If you ever need a clean re-derive of the
derived columns:

```bash
sqlite3 state/mesh.db "UPDATE mesh_tasks SET reply_text=NULL, prompt=NULL;"
python scripts/backfill_conversation_turns.py
```

## 3. Verify parity independently (the go/no-go gate)

```bash
python scripts/backfill_conversation_turns.py --verify   # parity only, no writes
```

Must print `mismatches=0 -> PASS`. The single known exception across the corpus is
the synthetic `task_recover01` fixture (no artifact, no history) — ignore it.

Spot-check a heavy claude session in the UI (chat + Files tab + Info tab) and confirm
full replies render.

## 4. Cut the live source over (already done in code)

No action — once deployed, the live paths are canonical:
- **Write:** `orchestrator._mesh_complete_task` writes full `reply_text` + prompt +
  parsed_output + file_changes + usage into `mesh_tasks` (DB-first, untruncated).
- **Read (chat):** `transcript.get_transcript` → `mesh_tasks` (file fallback only for
  un-enriched sessions).
- **Read (Files/Info tab):** `/api/artifacts*` → `mesh_tasks` (file fallback only when
  a task isn't in the DB).

## 5. Shrink new artifact files (optional, recommended)

Set in config (`config/settings.py` SystemConfig or your env override):

```python
slim_artifacts: bool = True
```

New completions then write only `results/raw/<task_id>.ndjson.gz` (the debug NDJSON,
~10x smaller) and drop `raw_stdout` from the JSON. The DB already has everything
product-facing. Restart the gateway.

## 6. Reclaim space (final, after a grace window)

After confirming the UI is healthy on DB-only data for a few days:

```bash
# Optional: archive raw streams you want to keep, gzipped, before deleting fat JSON
mkdir -p results/_archive
# (keep results/raw/*.ndjson.gz — that's your debug trace)
# Delete the fat per-task JSON (DB is canonical now):
find results -maxdepth 1 -name 'task_*.json' -delete
rm -f results/index.json                 # the file-based artifact index (unused once DB-first)
```

The app boots, serves chat, Files, and Info tabs, and survives with the fat artifact
JSON gone. `raw_stdout` debug archive (`results/raw/`) is kept and decodable anytime.

---

## Rollback

Before step 6 (delete), rollback is trivial: the files are untouched and every read
path falls back to them. To force the old behavior, you can revert the code; the new
DB columns are additive and harmless.

After step 6, restore from `state/mesh.db.bak-*` only if you also need the raw streams
back (otherwise the DB is the source and nothing is lost).
```
