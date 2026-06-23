/**
 * Fixtures — UI-0 deliverable (spec §14 Phase 0: session/task/approval/failure/
 * reconnect fixtures). Used by UI-1 to render Timeline + Tasks (🔵 MOCK-OK) and
 * by adapter unit tests.
 *
 * Two flavours:
 *   - `raw*` : exact backend payloads (../transport/rawApi shapes) — feed the
 *              adapters, prove the snake→dotted translation end to end.
 *   - canonical fixtures (sessions/tasks/…) : ../domain shapes the UI binds.
 *
 * No ⛔-dropped concepts appear (no tool executions, no progress, no archived).
 */
export * from "./rawFixtures";
export * from "./sessions";
export * from "./tasks";
export * from "./approvals";
export * from "./timeline";
export * from "./reconnect";
