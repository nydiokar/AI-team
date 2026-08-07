# Session Kept Pins Design

Date: 2026-08-07

## Intent

The operator needs a way to mark a chat session as intentionally kept for later,
without changing session ordering or runtime routing. The mark must survive close
and restart, carry a searchable note explaining why it was kept, and be visible
from the web UI.

This is not mesh node affinity. The codebase already uses "pinned session" for
`machine_id` routing, so persistence uses `keep_pinned` / `keep_note` while the UI
can still render a pin icon.

## Current Behavior

- `/api/sessions` returns `SessionView` rows from `SessionService.list_views`.
- Session list ordering is `updated_at DESC` from `SessionStore.list_all` /
  `MeshDB.list_sessions`.
- Closing a session sets `status = closed`; it does not delete the session record.
- Empty closed-session pruning can delete closed rows with no durable evidence.
- No session model, API field, or web domain field stores the operator's reason
  for keeping a session.

## Minimal Design

1. Add fields to `Session`: `keep_pinned: bool` and `keep_note: str`.
2. Persist both fields in JSON session records and mirror them to SQLite through
   one additive migration.
3. Expose both fields through `SessionView` and the existing `/api/sessions`
   response. `/api/sessions?keep_pinned=true` returns the kept subset directly,
   so old closed kept sessions are not missed by client-side filtering of the
   normal page.
4. Add `POST /api/sessions/{session_id}/keep` with a bounded JSON body:
   `{ "keep_pinned": boolean, "keep_note": string | null }`.
5. Add `SessionService.set_keep(...)` as the transport-neutral lifecycle seam.
6. Keep the DB `updated_at DESC` ordering unchanged (`touch=False` on keep
   writes). Surfacing is a *presentation* concern: the frontend groups kept
   sessions into a dedicated top section rather than reordering the store.
7. Prevent empty closed-session pruning from deleting `keep_pinned` sessions.
8. Frontend (UX):
   - the pin is a **one-tap optimistic toggle** on the row and in the detail
     menu — no modal to keep/unkeep; the cache is patched immediately and rolled
     back on error;
   - kept sessions surface in a dedicated collapsible **"Kept" section** at the
     top of the Sessions screen, pulled out of the normal groups so they never
     appear twice — this is what makes "find it later" actually work;
   - the note is an *optional* enrichment edited in a lightweight sheet (no
     checkbox gate; always-editable textarea; inline error) reached from the
     detail menu; Save keeps + writes the note, "Remove keep" clears both;
   - the server-backed **kept-only filter** remains as the exhaustive archive
     view (includes closed pins beyond the normal page limit);
   - search includes keep notes; keep note shown in Info.

## Service Boundary Check

- Concurrency: `set_keep` is a single session read plus one save/upsert. Last
  write wins, matching existing model/effort setters.
- Memory at scale: request body is capped at 4096 bytes; list reads remain
  bounded by `/api/sessions?limit=...`, and kept-only retrieval filters in the
  DB/read-store before applying that bound.
- Request size: endpoint rejects bodies over 4096 bytes.
- Timeout: no backend/worker calls; only local session store/DB write.
- Malformed input: Pydantic validates types; note is normalized and clamped.
- Backing resources: if the session is missing, return 404; DB shadow write
  failure follows existing `SessionStore.save` behavior with JSON persistence.

## Adversarial Review

- Naming collision avoided: storage does not use `pinned` alone.
- Ordering preserved: no ORDER BY change and keep-note writes use
  `SessionStore.save(..., touch=False)`, so marking a session kept does not move
  it to the top of the store. Surfacing is done purely in the frontend as a
  separate "Kept" section, so the store stays the single ordering authority while
  the operator still gets one-glance retrieval.
- Persistence through close is covered because close does not touch keep fields.
- Empty closed-session cleanup is adjusted so an intentionally kept empty session
  is not pruned.
- Notes are plain text; no HTML rendering path is introduced.
- Bounded payload avoids accidental huge localStorage/API writes.
