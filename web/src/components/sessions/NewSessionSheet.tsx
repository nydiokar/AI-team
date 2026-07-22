/**
 * New session sheet — guided wizard that mirrors the Telegram /session_new flow:
 * pick a backend → (node when mesh workers exist) → pick repo from auto-discovered
 * list → create. Falls back to a free-text path input so power users can type
 * absolute paths. Node selector appears only when live remote workers are online.
 *
 * The SAME wizard now hosts three intents behind one native flow (same UX for all):
 *   • Bare / Worker — create a plain (or Worker-profile) session (POST /api/sessions).
 *   • Manager       — fire the autonomous Manager loop on this repo, with an intent
 *                     + optional done-gate (POST /api/manager). This replaces the
 *                     bespoke, hard-to-find InvokeManagerSheet — a Manager is now
 *                     just a role you pick, exactly like a Worker.
 *   • Fork          — continue a stalled session as a fresh session. This is the
 *                     ORDINARY create flow (same backend→node→repo pick, same mesh
 *                     support), just pre-filled from the source, that records a
 *                     session→session lineage (`continued_from`) and carries the
 *                     marked-message digest onto its first instruction. It is a pure
 *                     SESSION action — it never touches Case membership or role.
 */
import { useState, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { X, ChevronRight, FolderOpen } from "lucide-react";
import { Button } from "../ui/Button";
import {
  useCreateSession,
  useInvokeManager,
} from "../../hooks/useSessionActions";
import { useProjects, useTargets } from "../../hooks/useLiveData";
import { useForkStore } from "../../stores/forkStore";
import { useManagerBootStore } from "../../stores/managerBootStore";
import { cn } from "../../lib/cn";

const BACKENDS = [
  { id: "claude", label: "Claude Code", icon: "🧠" },
  { id: "codex", label: "Codex", icon: "🤖" },
  { id: "opencode", label: "OpenCode", icon: "🛠" },
  { id: "opencode-server", label: "OpenCode Server", icon: "🛰" },
] as const;

type Step = "backend" | "node" | "repo";
export type SessionRole = "bare" | "worker" | "manager";

/** [Session-fork] Context handed to the sheet when it is opened as a fork. The
 *  digest is the verbatim marked-message transcript; it is stashed client-side and
 *  attached to the new session's first instruction (never pasted as a message). */
export interface ForkContext {
  sourceSessionId: string;
  digest: string;
  backend: string;
  nodeId: string;
  repoPath: string;
  model?: string | null;
}

export function NewSessionSheet({
  onClose,
  initialRole,
  fork,
}: {
  onClose: () => void;
  /** Preselect a role (e.g. "manager" when opened from the Work screen). */
  initialRole?: SessionRole;
  /** When present, the sheet forks `fork.sourceSessionId` instead of creating. */
  fork?: ForkContext;
}) {
  const navigate = useNavigate();
  const create = useCreateSession();
  const invoke = useInvokeManager();
  const setCarry = useForkStore((s) => s.setCarry);
  const setBoot = useManagerBootStore((s) => s.setBoot);

  const isFork = !!fork;
  // A fork is the ordinary create flow, pre-filled from the source — the FULL
  // wizard (backend → node → repo), so it works on the mesh exactly like "+".
  const [step, setStep] = useState<Step>("backend");
  const [backend, setBackend] = useState<string>(fork?.backend ?? "claude");
  const [nodeId, setNodeId] = useState<string>(fork?.nodeId ?? "__local__");
  const [repoPath, setRepoPath] = useState<string>(fork?.repoPath ?? "");
  // Role is ORTHOGONAL to fork. A fork carries prior context (lineage + marked
  // digest); the role (bare / worker / manager) is a separate behavioral+tools axis.
  // Any role can be forked — bare/worker via the create pipeline, manager via
  // /api/manager (which now accepts the same fork seed). Default bare.
  const [role, setRole] = useState<SessionRole>(initialRole ?? "bare");
  const [objective, setObjective] = useState("");
  const [criteria, setCriteria] = useState("");
  const [manualMode, setManualMode] = useState(false);
  const manualRef = useRef<HTMLInputElement>(null);

  const { data: targets } = useTargets();
  const liveRemoteNodes = (targets ?? []).filter(
    (t) => t.id !== "__local__" && t.live,
  );
  const hasRemoteNodes = liveRemoteNodes.length > 0;

  const { data: projects, isLoading: projectsLoading } = useProjects(nodeId);

  const isManager = role === "manager";
  const pending = create.isPending || invoke.isPending;
  const anyError = create.isError || invoke.isError;
  const errorMsg = create.error?.message ?? invoke.error?.message;

  // Only a Manager needs an explicit confirm (it also needs an intent), so tapping a
  // repo only SELECTS it there. Bare / Worker / Fork keep the one-tap "pick repo =
  // go" flow — a fork carries its context automatically, so no extra input.
  const needsExplicitSubmit = isManager || manualMode;

  const chooseBackend = (b: string) => {
    setBackend(b);
    if (hasRemoteNodes) {
      setStep("node");
    } else {
      setNodeId("__local__");
      setStep("repo");
    }
  };

  const chooseNode = (nid: string) => {
    setNodeId(nid);
    setStep("repo");
  };

  const chooseRepo = (path: string) => {
    setRepoPath(path);
    if (!needsExplicitSubmit) submitWith(path);
  };

  const submitWith = (path: string) => {
    const p = path.trim();
    if (!p || pending) return;

    if (isManager) {
      const obj = objective.trim();
      if (!obj) return;
      // [Manager-fork] A forked Manager carries the SAME seed as a bare/worker fork —
      // lineage + the marked digest — but the context rides in server-side on the
      // Manager's first assignment turn (invoke_manager), NOT deferred to a Composer
      // send. So there is no setCarry here: the seed is delivered at boot.
      invoke.mutate(
        {
          objective: obj,
          repoPath: p,
          backend,
          nodeId,
          completionCriteria: criteria.trim() || undefined,
          continuedFrom: isFork ? fork!.sourceSessionId : undefined,
          continueInline: isFork ? fork!.digest : undefined,
        },
        {
          onSuccess: (res) => {
            // Stash an optimistic boot record so the session timeline shows the
            // assignment (goal + any forked prior context) IMMEDIATELY — the real
            // turn is delivered server-side and only lands in the transcript once
            // the boot turn completes, otherwise the operator is blind meanwhile.
            if (res?.session_id) {
              setBoot(res.session_id, {
                objective: obj,
                hasPriorContext: isFork,
                createdAt: new Date().toISOString(),
              });
            }
            onClose();
            if (res?.case_id) navigate(`/work/${res.case_id}`);
            else if (res?.session_id) navigate(`/sessions/${res.session_id}`);
          },
        },
      );
      return;
    }

    // Bare / Worker / Fork all go through the ordinary create pipeline (mesh-capable).
    // A fork just adds `continuedFrom` (session lineage) and stashes the marked-message
    // digest so the new session's FIRST instruction carries it in.
    create.mutate(
      {
        backend,
        repoPath: p,
        nodeId,
        model: isFork ? fork!.model ?? undefined : undefined,
        roleBoot: role === "worker" ? "worker" : undefined,
        continuedFrom: isFork ? fork!.sourceSessionId : undefined,
      },
      {
        onSuccess: (env) => {
          const id = env.session?.session_id;
          if (id && isFork && fork) setCarry(id, { continueInline: fork.digest });
          onClose();
          if (id) navigate(`/sessions/${id}`);
        },
      },
    );
  };

  const submit = () => submitWith(repoPath);
  const submitDisabled =
    !repoPath.trim() || pending || (isManager && !objective.trim());

  const backendLabel = BACKENDS.find((b) => b.id === backend)?.label ?? backend;
  const headerTitle = isFork
    ? isManager
      ? "Continue as Manager"
      : "Continue in a new session"
    : isManager
      ? "Invoke Manager"
      : "New session";
  const submitLabel = isManager
    ? invoke.isPending ? "Invoking…" : "Invoke Manager"
    : create.isPending ? "Creating…" : "Create session";

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/50"
      onClick={onClose}
    >
      <div
        className="card-elev w-full max-w-[480px] rounded-t-2xl p-5 pb-8"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            {step !== "backend" && (
              <button
                onClick={() => setStep(step === "repo" && hasRemoteNodes ? "node" : "backend")}
                className="flex size-7 items-center justify-center rounded-full text-ink-soft hover:bg-surface-2"
                aria-label="Back"
              >
                <ChevronRight className="size-4 rotate-180" />
              </button>
            )}
            <h2 className="text-base font-semibold text-ink">
              {step === "backend" && headerTitle}
              {step === "node" && `${backendLabel} — choose machine`}
              {step === "repo" && (isFork || isManager ? headerTitle : `${backendLabel} — pick repo`)}
            </h2>
          </div>
          <button
            onClick={onClose}
            className="flex size-8 items-center justify-center rounded-full text-ink-soft hover:bg-surface-2"
            aria-label="Close"
          >
            <X className="size-5" />
          </button>
        </div>

        {isFork && (
          <p className="mt-1 mb-3 text-xs text-ink-muted">
            A fresh session (fresh cache) that continues this thread — pick backend,
            machine and repo like any new session. Your marked messages ride in on the
            first instruction, and it's linked back to the session you continued.
          </p>
        )}

        {/* Step 1: Backend */}
        {step === "backend" && (
          <>
            <p className="mt-1 mb-3 text-xs text-ink-muted">
              Choose the AI backend for this session.
            </p>
            <div className="flex flex-col gap-2">
              {BACKENDS.map((b) => (
                <button
                  key={b.id}
                  onClick={() => chooseBackend(b.id)}
                  className="flex items-center gap-3 rounded-xl border border-hairline bg-surface-1 px-4 py-3 text-[14px] text-ink hover:bg-surface-2"
                >
                  <span className="text-lg">{b.icon}</span>
                  <span className="font-medium">{b.label}</span>
                  <ChevronRight className="ml-auto size-4 text-ink-muted" />
                </button>
              ))}
            </div>
          </>
        )}

        {/* Step 2: Node (only when remote workers) */}
        {step === "node" && (
          <>
            <p className="mt-1 mb-3 text-xs text-ink-muted">
              Which machine should run this session?
            </p>
            <div className="flex flex-col gap-2">
              <button
                onClick={() => chooseNode("__local__")}
                className="flex items-center gap-3 rounded-xl border border-hairline bg-surface-1 px-4 py-3 text-[14px] text-ink hover:bg-surface-2"
              >
                <span className="text-lg">🖥</span>
                <span className="font-medium">This server (local)</span>
                <ChevronRight className="ml-auto size-4 text-ink-muted" />
              </button>
              {liveRemoteNodes.map((t) => (
                <button
                  key={t.id}
                  onClick={() => chooseNode(t.id)}
                  className="flex items-center gap-3 rounded-xl border border-hairline bg-surface-1 px-4 py-3 text-[14px] text-ink hover:bg-surface-2"
                >
                  <span className="text-lg">🌐</span>
                  <div className="min-w-0 flex-1 text-left">
                    <p className="font-medium">{t.id}</p>
                    {t.tailscaleIp && (
                      <p className="text-[11px] font-mono text-ink-muted">{t.tailscaleIp}</p>
                    )}
                  </div>
                  <ChevronRight className="size-4 text-ink-muted" />
                </button>
              ))}
            </div>
          </>
        )}

        {/* Step 3: Repo */}
        {step === "repo" && (
          <>
            {/* Role — orthogonal to fork. Bare (plain), Worker (worker profile), or
                Manager (drives workers). Shown for BOTH a fresh session and a fork:
                a fork can wake in any role and still carry its prior context (lineage
                + marked digest). Manager forks route through /api/manager, which now
                accepts the same fork seed and delivers it on the first assignment. */}
            <div className="mb-3 mt-1">
              <p className="mb-1.5 text-xs text-ink-muted">
                {isFork ? "Continue as" : "Session role"}
              </p>
              <div className="flex gap-2">
                {([
                  { id: "bare", label: "Bare", hint: "plain session" },
                  { id: "worker", label: "Worker", hint: "worker profile" },
                  { id: "manager", label: "Manager", hint: "drives workers" },
                ] as const).map((r) => (
                  <button
                    key={r.id}
                    onClick={() => setRole(r.id)}
                    className={cn(
                      "flex-1 rounded-xl border px-2.5 py-2 text-left text-[13px] transition",
                      role === r.id
                        ? "border-accent/40 bg-accent-dim/40 text-ink ring-1 ring-accent/30"
                        : "border-hairline bg-surface-1 text-ink hover:bg-surface-2",
                    )}
                  >
                    <span className="font-medium">{r.label}</span>
                    <span className="ml-1 text-[10px] text-ink-muted">{r.hint}</span>
                  </button>
                ))}
              </div>
            </div>

            {/* Manager intent + done-gate — the same fields the old sheet had, now
                native to the create flow. */}
            {isManager && (
              <div className="mb-3">
                <label className="mb-1 block text-xs text-ink-muted">Intent</label>
                <textarea
                  value={objective}
                  onChange={(e) => setObjective(e.target.value)}
                  rows={2}
                  placeholder="continue the work on this project"
                  className="mb-2 w-full resize-none rounded-lg border border-hairline bg-surface-1 px-3 py-2.5 text-[14px] text-ink placeholder:text-ink-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
                />
                <label className="mb-1 block text-xs text-ink-muted">
                  Done gate <span className="text-ink-muted/70">(optional)</span>
                </label>
                <input
                  value={criteria}
                  onChange={(e) => setCriteria(e.target.value)}
                  placeholder="what 'done' means — demanded at close"
                  className="w-full rounded-lg border border-hairline bg-surface-1 px-3 py-2.5 text-[13px] text-ink placeholder:text-ink-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
                />
              </div>
            )}

            <p className="mt-1 mb-3 text-xs text-ink-muted">
              {nodeId === "__local__" ? "Repos found in your workspace:" : `Repos on ${nodeId}:`}
            </p>

            {/* Auto-discovered list */}
            {!manualMode && (
              <>
                {projectsLoading && (
                  <p className="py-4 text-center text-sm text-ink-muted">Scanning repos…</p>
                )}
                {!projectsLoading && (projects ?? []).length === 0 && (
                  <p className="py-2 text-center text-sm text-ink-muted">
                    No repos found.{" "}
                    <button
                      className="text-accent underline"
                      onClick={() => { setManualMode(true); setTimeout(() => manualRef.current?.focus(), 50); }}
                    >
                      Type a path
                    </button>
                  </p>
                )}
                <div className="mb-3 max-h-52 overflow-y-auto space-y-1.5">
                  {(projects ?? []).map((p) => (
                    <button
                      key={p.path}
                      onClick={() => chooseRepo(p.path)}
                      disabled={pending}
                      className={cn(
                        "flex w-full items-center gap-3 rounded-xl border px-4 py-2.5 text-left text-[13px] transition disabled:opacity-50",
                        repoPath === p.path
                          ? "border-accent/40 bg-accent-dim/40 text-ink ring-1 ring-accent/30"
                          : "border-hairline bg-surface-1 text-ink hover:bg-surface-2",
                      )}
                    >
                      <FolderOpen className="size-4 shrink-0 text-ink-muted" />
                      <div className="min-w-0">
                        <p className="font-medium truncate">{p.name}</p>
                        <p className="truncate font-mono text-[10px] text-ink-muted">{p.path}</p>
                      </div>
                    </button>
                  ))}
                </div>

                {(projects ?? []).length > 0 && (
                  <button
                    className="mb-3 text-xs text-accent hover:underline"
                    onClick={() => { setManualMode(true); setTimeout(() => manualRef.current?.focus(), 50); }}
                  >
                    Or type a path manually
                  </button>
                )}
              </>
            )}

            {/* Manual path input */}
            {manualMode && (
              <>
                <input
                  ref={manualRef}
                  value={repoPath}
                  onChange={(e) => setRepoPath(e.target.value)}
                  placeholder="C:/Users/you/Projects/your-repo"
                  className="mb-2 w-full rounded-lg border border-hairline bg-surface-1 px-3 py-2.5 font-mono text-[13px] text-ink placeholder:text-ink-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
                  onKeyDown={(e) => e.key === "Enter" && submit()}
                />
                {(projects ?? []).length > 0 && (
                  <button
                    className="mb-3 text-xs text-accent hover:underline"
                    onClick={() => setManualMode(false)}
                  >
                    ← Back to list
                  </button>
                )}
              </>
            )}

            {anyError && (
              <p className="mb-3 text-[12px] text-bad">
                Couldn't {isFork ? "fork" : isManager ? "invoke" : "create"}: {String(errorMsg ?? "unknown")}.
              </p>
            )}

            {(needsExplicitSubmit) && (
              <Button
                className="w-full"
                disabled={submitDisabled}
                onClick={submit}
              >
                {submitLabel}
              </Button>
            )}
          </>
        )}
      </div>
    </div>
  );
}
