# UI-4 — Files & artifacts (the phone review loop)

Scope fence per COCKPIT_REFACTOR_SPEC §13: **only the boxes below are in scope.**
Baseline: branch feat/webui-ui0 @ 3dc00c7 (H/UI-3). Backend deps in system python.

Spec row (§14, §7.6): *"Artifact cards + unified diff review bind to a real
listing endpoint."* Today `FilesScreen.tsx` is pure mock — the data exists on disk
(`results/<task_id>.json`) but there is no listing API. UI-4 is a two-part job:
(1) a backend artifact/files listing API in control_api.py, then (2) bind
`FilesScreen` + an artifact-card / file-change view to it.

## Design decision (what we model — and what we do NOT invent)
Real sources, verified against disk on 2026-06-24:
- `results/<task_id>.json` — the full task artifact. Universal fields used:
  `task_id, success, timestamp, files_modified[] (string paths), execution_time,
  parent_task_id, errors[]`.
- `file_changes[]` — RICHER per-file shape, present in ~9% of artifacts
  (`{path, git_status, change_type, added_lines, deleted_lines}`). When present we
  surface it; otherwise we derive a flat `RemoteFile` from `files_modified` with
  `change:"modified"` (we do NOT guess added/deleted without data).
- `results/index.json` — task_id → artifact path map (orchestrator's own index).
- `SessionView.last_files_modified` (already on the wire) + `session.last_artifact_path`
  — already reachable via /api/sessions; UI-4 does NOT duplicate them, it links
  the artifact list to its owning session.

We do NOT invent: a diff *body* (no artifact stores a unified-diff hunk today —
the "unified diff view" renders the per-file change rows we DO have, not fabricated
hunks), file content preview (no content is stored), upload, or any new field not
above. Diff hunks / content preview are explicitly OUT (no backend source).

Path-traversal: the only file read is `results/<id>.json` for a single artifact —
the `<id>` is confined to `results/` exactly like the SPA file resolver
(`_mount_web_ui`), rejecting any `..`/absolute escape. The list endpoint reads the
`results/` dir + `index.json` only.

---

## Box 1 — backend: artifact reader (pure, testable)
- [ ] `src/control/artifacts.py`: pure helpers, no FastAPI import.
  - `list_artifacts(results_dir, limit) -> list[dict]` — scan `results/*.json`
    (skip `index.json`), newest-first by mtime, read each shallow → summary dict
    `{task_id, artifact_path, success, timestamp, file_count, files_modified[],
    has_changes(bool), session_id?, parent_task_id?}`. Bounded by `limit`.
  - `get_artifact(results_dir, task_id) -> dict|None` — confined read of one
    `results/<task_id>.json` (reject traversal; return None on escape/missing).
    Returns `{task_id, success, timestamp, execution_time, errors[],
    files_modified[], file_changes[]|None, parent_task_id}` — the full per-file
    detail for the card/diff view. Raw stdout/stderr NOT surfaced (UI-5 logs).
  - `to_remote_files(artifact) -> list[dict]` — `file_changes[]` if present (carry
    change_type→change + line counts), else `files_modified[]` → `{path,
    change:"modified", added:null, deleted:null}`.
- [ ] Unit tests `tests/test_artifacts.py`: list newest-first + limit; get confined
  (traversal `../../.env` → None); file_changes preferred over files_modified;
  missing id → None.

Done = exactly: the module + tests. No HTTP, no orchestrator edit.
Do NOT touch: orchestrator, db.py, view_models.
Revert: delete artifacts.py + test.

## Box 2 — backend: HTTP surface (control_api)
- [ ] GET `/api/artifacts?limit=N` → `{artifacts:[summary…]}` (auth, like /api/tasks).
- [ ] GET `/api/artifacts/{task_id}` → `{artifact:{…}, files:[RemoteFile…]}`;
  404 (`not_found`) when the confined read returns None.
- [ ] Resolve results_dir from `config.system.results_dir` (no hardcode).
- [ ] Tests in test_control_api.py: list returns array; get known id → 200 w/ files;
  get traversal/missing → 404; auth required.

Done = exactly: the 2 endpoints + tests.
Do NOT touch: instruction/session/approval endpoints.
Revert: drop the routes.

## Box 3 — frontend: raw shape → adapter → hook
- [ ] rawApi: `RawArtifactSummary`, `RawArtifactDetail`, `RawRemoteFile` (exact
  backend shapes).
- [ ] apiClient: `artifacts(limit)` + `artifact(taskId)` (GET, Bearer).
- [ ] artifactAdapter: `toArtifacts(raw[])` → domain `Artifact[]`;
  `toArtifactDetail` → `{artifact, files: RemoteFile[]}` (RemoteFile already in
  models.ts). Map change_type/git_status → RemoteFile.change.
- [ ] useArtifacts() (poll) + useArtifact(taskId) hooks in useLiveData.ts.
- [ ] artifactAdapter test (change mapping + flat fallback).

Done = exactly: raw + client + adapter + hooks + adapter test.
Do NOT touch: session/task/approval adapters.
Revert: delete the 3 files' additions.

## Box 4 — frontend: bind FilesScreen + artifact card / file-change view
- [ ] FilesScreen: list artifact cards from useArtifacts (task id, success pill,
  time, file count, link to owning session when present). Replace the mock.
- [ ] Artifact detail: expand a card → per-file change rows (path + change badge +
  ±lines when known) = the "unified diff review" over the data we have.
- [ ] Empty / loading / error states matching the other screens.
- [ ] tsc + vitest + vite build green.

Done = exactly: FilesScreen shows real artifacts + their changed files from the phone.
Do NOT touch: Composer, event stream, Tasks/Approvals screens.
Revert: restore the placeholder FilesScreen.

## Gate
- [x] Backend pytest green (7 test_artifacts + 3 control_api artifacts; 28 total
  in those two files, no regression).
- [x] Frontend `tsc` clean + vitest 23 (+2 artifactAdapter) + `vite build` green.
- [x] Live (2026-06-24): GATEWAY_TELEGRAM_BOT_TOKEN="" MESH_ENABLED=false
  python main.py — Telegram OFF, control API up on 9003. GET /api/artifacts →
  real results newest-first (no-auth → 403); GET /api/artifacts/task_025fd90a →
  file_changes `untracked`→`added` +241/0; flat files_modified artifact also
  normalized; missing id → 404; SPA /files still 200.

ALL BOXES DONE 2026-06-24. FilesScreen shows real artifacts + their changed files
from the phone (the core "what did the agent change?" review loop). Diff hunks /
file-content preview remain OUT (no backend source) — UI-5 (logs) is next.

### Adversarial review (2026-06-24, post-build)
- **Path confinement is load-bearing — verified, not assumed.** A *drive-absolute*
  task_id (`C:/Windows/.../hosts`) makes `Path('results') / 'C:/…'` DISCARD the left
  operand and resolve OUTSIDE results/ — the `base in candidate.parents` check is the
  only thing stopping it. Confirmed empirically; added regression coverage
  (drive-absolute, `/etc/passwd`, ``""``, ``.``) to test_artifacts::*traversal.
- `file_count` (summary) and the expanded RemoteFile rows use the SAME precedence
  (file_changes wins over files_modified), so they never disagree — checked.
- Timestamp `slice(0,10)` is truthiness-guarded (empty string falsy) and handles the
  non-ISO `"YYYY-MM-DD HH:MM:SS"` form too. Card `<Link>` is a sibling of the toggle
  `<button>`, not nested — valid HTML.
- 3s poll on the immutable artifact list is wasteful but matches the established
  POLL_MS pattern (sessions/tasks/approvals); left for consistency.
