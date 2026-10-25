import { BackendAccountSheet, type AccountBackend } from "../home/BackendAccountSheet";
import { useBackendAccountStatus } from "../../hooks/useBackendAccountStatus";
import { useTargets } from "../../hooks/useLiveData";
import { useAuthStore } from "../../stores/authStore";

export function SessionAccountPanel({ backend, nodeId, open, onOpen, onClose }: {
  backend: AccountBackend; nodeId: string; open: boolean; onOpen: () => void; onClose: () => void;
}) {
  const token = useAuthStore((s) => s.token);
  const targets = useTargets();
  // Native credentials belong to the session's carrier. Never select another
  // node merely because it is online or has the same backend installed.
  const nodes = (targets.data ?? []).filter((node) => node.id === nodeId);
  const node = nodes[0];
  const account = useBackendAccountStatus(token, nodeId, backend, Boolean(node?.live) && !open);
  const state = node && !node.live ? "offline" : account.data?.state;
  const provider = backend === "claude" ? "Claude" : "Codex";
  return <>
    <div className="flex items-center justify-between gap-2 border-t border-hairline px-3 py-2 text-xs">
      <span role="status" className={state === "signed_out" ? "text-warn" : "text-ink-muted"}>
        {provider} · {nodeId || "node not assigned"} · {state === "signed_out" ? "Signed out — sign in to work" : state === "signed_in" ? "Signed in" : state === "offline" ? "Node offline" : account.isFetching ? "Checking account…" : "Account status unavailable"}
      </span>
      <button onClick={onOpen} className="shrink-0 rounded-lg border border-hairline px-3 py-2">{state === "signed_out" ? "Sign in" : "Account"}</button>
    </div>
    {open && <BackendAccountSheet backend={backend} nodes={nodes} onClose={onClose} />}
  </>;
}
