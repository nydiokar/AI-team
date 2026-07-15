/**
 * Write actions (UI-2 / Move F) — the mutation half of the live gate:
 * "send instruction → ack states; stop/retry work."
 *
 * Each mutation:
 *   • mints ONE Idempotency-Key per attempt (a TanStack retry reuses it, so a
 *     network blip never double-submits — backend dedupes on it);
 *   • exposes the command-delivery ack state (draft→sending→acknowledged|rejected,
 *     domain/status CommandDeliveryState) so the composer can show progress;
 *   • invalidates the sessions/tasks queries on settle so the polled read state
 *     reconciles with the new server truth (whole-message, no streaming).
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, newIdempotencyKey } from "../transport/apiClient";
import { useAuthStore } from "../stores/authStore";

/** What the UI shows about an in-flight command (spec §9.2). */
export type DeliveryState = "idle" | "sending" | "acknowledged" | "rejected";

/**
 * Send an instruction to a session (or one-off). Returns a TanStack mutation; the
 * caller reads `.status`/`.error` to drive the composer ack chip. The idempotency
 * key is bound to the variables so retries of the SAME logical send reuse it.
 */
export function useSubmitInstruction() {
  const token = useAuthStore((s) => s.token);
  const qc = useQueryClient();

  return useMutation({
    mutationFn: (vars: {
      description: string;
      sessionId?: string;
      idempotencyKey?: string;
    }) =>
      api.submitInstruction(
        token,
        { description: vars.description, sessionId: vars.sessionId },
        vars.idempotencyKey ?? newIdempotencyKey(),
      ),
    // Bad token / malformed request won't fix itself; only retry transient faults.
    retry: (count, err) =>
      !(err instanceof ApiError && err.status >= 400 && err.status < 500) && count < 2,
    onSettled: (_data, _err, vars) => {
      qc.invalidateQueries({ queryKey: ["sessions"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      if (vars.sessionId) {
        qc.invalidateQueries({ queryKey: ["session", vars.sessionId] });
      }
    },
  });
}

/**
 * Resolve a pending approval (Move H). Idempotency-keyed so a double-tap or retry
 * can't double-resolve (the backend guard also returns 409, but the key avoids
 * even sending the second request on a flaky network). Invalidates the approvals
 * queue + sessions on settle.
 */
export function useResolveApproval() {
  const token = useAuthStore((s) => s.token);
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { approvalId: string; decision: "approved" | "rejected" }) =>
      api.resolveApproval(token, vars.approvalId, vars.decision, newIdempotencyKey()),
    retry: false,
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["approvals"] });
      qc.invalidateQueries({ queryKey: ["sessions"] });
    },
  });
}

/** Stop the in-flight task on a session (control_api.api_stop_session). */
export function useStopSession() {
  const token = useAuthStore((s) => s.token);
  const qc = useQueryClient();

  return useMutation({
    mutationFn: (sessionId: string) => api.stopSession(token, sessionId),
    retry: false, // a stop is not safe to blindly auto-retry; user re-presses.
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["sessions"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}

/**
 * Create a new session (control_api.api_create_session). Web-origin, unbound.
 * Idempotency-keyed so a double-tap can't create two sessions. Invalidates the
 * sessions list so the new one appears.
 */
export function useCreateSession() {
  const token = useAuthStore((s) => s.token);
  const qc = useQueryClient();

  return useMutation({
    mutationFn: (vars: {
      backend: string;
      repoPath: string;
      nodeId?: string;
      model?: string;
      idempotencyKey?: string;
    }) =>
      api.createSession(
        token,
        { backend: vars.backend, repoPath: vars.repoPath, model: vars.model, nodeId: vars.nodeId },
        vars.idempotencyKey ?? newIdempotencyKey(),
      ),
    retry: (count, err) =>
      !(err instanceof ApiError && err.status >= 400 && err.status < 500) && count < 2,
    onSettled: () => qc.invalidateQueries({ queryKey: ["sessions"] }),
  });
}

/** Close a session (control_api.api_close_session). */
export function useCloseSession() {
  const token = useAuthStore((s) => s.token);
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (sessionId: string) => api.closeSession(token, sessionId),
    retry: false,
    onSettled: () => qc.invalidateQueries({ queryKey: ["sessions"] }),
  });
}

/** Restore a closed session (control_api.api_restore_session). */
export function useRestoreSession() {
  const token = useAuthStore((s) => s.token);
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (sessionId: string) => api.restoreSession(token, sessionId),
    retry: false,
    onSettled: () => qc.invalidateQueries({ queryKey: ["sessions"] }),
  });
}

/** Upload a file to the session's uploads/ directory. */
export function useUploadFile() {
  const token = useAuthStore((s) => s.token);
  return useMutation({
    mutationFn: (vars: { sessionId: string; file: File }) =>
      api.uploadFile(token, vars.sessionId, vars.file),
    retry: false,
  });
}

/** Compact the session's Claude context window (POST /api/sessions/{id}/compact). */
export function useCompactSession() {
  const token = useAuthStore((s) => s.token);
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (sessionId: string) => api.compactSession(token, sessionId),
    retry: false,
    onSettled: () => qc.invalidateQueries({ queryKey: ["sessions"] }),
  });
}

/** Set the model for a session (POST /api/sessions/{id}/model). */
export function useSetModel() {
  const token = useAuthStore((s) => s.token);
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { sessionId: string; model: string | null }) =>
      api.setModel(token, vars.sessionId, vars.model),
    retry: false,
    onSettled: () => qc.invalidateQueries({ queryKey: ["sessions"] }),
  });
}

/** Set the persistent thinking effort for a session. */
export function useSetEffort() {
  const token = useAuthStore((s) => s.token);
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { sessionId: string; effort: string | null }) =>
      api.setEffort(token, vars.sessionId, vars.effort),
    retry: false,
    onSettled: () => qc.invalidateQueries({ queryKey: ["sessions"] }),
  });
}

/** Run a repo inspection op on a session (git_status, list_dirs, commit, commit_all). */
export function useInspectSession() {
  const token = useAuthStore((s) => s.token);
  return useMutation({
    mutationFn: (vars: {
      sessionId: string;
      op: string;
      params?: Record<string, unknown>;
    }) => api.inspectSession(token, vars.sessionId, vars.op, vars.params),
    retry: false,
  });
}
