import { useEffect, useRef, useState, type FormEvent } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { api } from "../../transport/apiClient";
import { useAuthStore } from "../../stores/authStore";
import type { Target } from "../../domain/models";

export type AccountBackend = "claude" | "codex";

export function safeAuthUrl(value: string, backend: AccountBackend): boolean {
  try {
    const url = new URL(value);
    const hosts = backend === "claude" ? ["claude.ai", "console.anthropic.com", "platform.claude.com"] : ["auth.openai.com"];
    return url.protocol === "https:" && !url.username && !url.password && (!url.port || url.port === "443") && hosts.includes(url.hostname);
  } catch { return false; }
}

function terminalOutput(output: string, backend: AccountBackend) {
  return output.split(/(https:\/\/[^\s\x1b<>"']+)/g).map((part, index) => safeAuthUrl(part, backend)
    ? <a key={index} href={part} target="_blank" rel="noopener noreferrer" className="break-all text-accent underline">{part}</a>
    : part);
}

function NodeAccount({ node, backend }: { node: Target; backend: AccountBackend }) {
  const token = useAuthStore((s) => s.token);
  const [state, setState] = useState("loading");
  const [loginId, setLoginId] = useState("");
  const [running, setRunning] = useState(false);
  const [busy, setBusy] = useState(false);
  const [output, setOutput] = useState("");
  const [input, setInput] = useState("");
  const [error, setError] = useState("");
  const activeLogin = useRef("");
  const mounted = useRef(false);
  const operation = useRef(0);

  useEffect(() => {
    mounted.current = true;
    let active = true;
    void api.backendAuthStatus(token, node.id, backend).then((result) => {
      if (active) setState(result.state);
    }).catch(() => { if (active) setState("unavailable"); });
    return () => {
      active = false;
      mounted.current = false;
      operation.current++;
      if (activeLogin.current) void api.backendAuth(token, node.id, backend, "cancel", { login_id: activeLogin.current }).catch(() => undefined);
      activeLogin.current = "";
    };
  }, [token, node.id, backend]);

  useEffect(() => {
    if (!loginId || !running) return;
    let active = true;
    const current = operation.current;
    let timer: ReturnType<typeof setTimeout>;
    let offset = 0;
    const poll = async () => {
      try {
        const result = await api.backendAuth(token, node.id, backend, "poll", { login_id: loginId, offset });
        if (!active || current !== operation.current) return;
        offset = result.offset ?? offset;
        if (result.output) setOutput((old) => (old + result.output).slice(-65536));
        if (result.status === "exited") {
          activeLogin.current = "";
          setRunning(false);
          setState(result.authenticated === true ? "signed_in" : result.authenticated === false ? "signed_out" : "unavailable");
          return;
        }
        timer = setTimeout(() => void poll(), 700);
      } catch {
        if (active && current === operation.current) {
          setError("Login connection failed. Cancel and try again.");
          setRunning(false);
          setState("unavailable");
        }
      }
    };
    void poll();
    return () => { active = false; clearTimeout(timer); };
  }, [loginId, running, token, node.id, backend]);

  const start = async () => {
    const current = ++operation.current;
    setBusy(true); setError(""); setOutput("");
    try {
      const result = await api.backendAuth(token, node.id, backend, "start");
      if (!mounted.current || current !== operation.current) {
        void api.backendAuth(token, node.id, backend, "cancel", { login_id: result.login_id }).catch(() => undefined);
        return;
      }
      activeLogin.current = result.login_id;
      setLoginId(result.login_id); setRunning(true);
    } catch { if (mounted.current) setError("Could not start login. Check the node and try again."); }
    finally { if (mounted.current) setBusy(false); }
  };

  const cancel = async () => {
    operation.current++;
    setRunning(false); setBusy(true);
    try {
      await api.backendAuth(token, node.id, backend, "cancel", { login_id: activeLogin.current });
      activeLogin.current = ""; setLoginId(""); setError("");
    } catch { setError("Could not cancel login. Try again; it expires after ten minutes."); }
    finally { if (mounted.current) setBusy(false); }
  };

  const send = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true);
    try {
      await api.backendAuth(token, node.id, backend, "input", { login_id: loginId, input: `${input}\n` });
      if (mounted.current) setInput("");
    } catch { if (mounted.current) setError("Could not send login response."); }
    finally { if (mounted.current) setBusy(false); }
  };

  return <section aria-label={`${node.id} account`} className="space-y-2 border-t border-hairline py-4">
    <h3 className="font-semibold text-ink">{node.id}</h3>
    <p role="status" className="text-sm text-ink-soft">{state === "signed_in" ? "Signed in" : state === "signed_out" ? "Signed out" : state === "loading" ? "Checking account…" : "Account status unavailable"}</p>
    {activeLogin.current ? <button disabled={busy} onClick={() => void cancel()} className="rounded-lg border border-hairline px-3 py-2">Cancel login</button>
      : <button disabled={busy || state === "loading"} onClick={() => void start()} className="rounded-lg bg-accent px-3 py-2 text-white disabled:opacity-50">{busy ? "Starting…" : state === "signed_in" ? "Sign in again" : "Log in"}</button>}
    {loginId && <>
      <p className="text-xs text-ink-muted">{running ? backend === "claude" ? "Open the auth link, then paste the returned code below." : "Open the auth link and enter the device code. Your workspace administrator may need to enable device-code authentication." : "Login ended."}</p>
      <pre aria-live="polite" className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-surface-2 p-3 text-xs">{terminalOutput(output.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, ""), backend)}</pre>
      {running && backend === "claude" && <form onSubmit={(e) => void send(e)} className="flex gap-2">
        <input aria-label="Claude login response" value={input} maxLength={1000} onChange={(e) => setInput(e.target.value)} className="min-w-0 flex-1 rounded-lg bg-surface-2 p-2" />
        <button disabled={busy} type="submit" className="rounded-lg border border-hairline p-2">Send</button>
      </form>}
    </>}
    {error && <p role="alert" className="text-xs text-bad">{error}</p>}
  </section>;
}

export function BackendAccountSheet({ backend, nodes, onClose }: { backend: AccountBackend; nodes: Target[]; onClose: () => void }) {
  const eligible = nodes.filter((n) => n.live && n.backends.includes(backend));
  return <Dialog.Root open onOpenChange={(open) => { if (!open) onClose(); }}>
    <Dialog.Portal>
      <Dialog.Overlay className="fixed inset-0 z-50 bg-black/50" />
      <Dialog.Content className="card-elev fixed bottom-0 left-1/2 z-50 max-h-[85vh] w-full max-w-[480px] -translate-x-1/2 overflow-y-auto rounded-t-2xl p-5 pb-8">
        <div className="mb-3 flex items-center justify-between">
          <Dialog.Title className="text-base font-semibold capitalize">{backend} accounts</Dialog.Title>
          <Dialog.Close aria-label="Close accounts" className="rounded-lg border border-hairline px-3 py-2">Close</Dialog.Close>
        </div>
        <Dialog.Description className="mb-3 text-sm text-ink-muted">Accounts on online nodes supporting {backend}.</Dialog.Description>
        {eligible.length ? eligible.map((node) => <NodeAccount key={`${backend}:${node.id}`} node={node} backend={backend} />) : <p>No supporting nodes online.</p>}
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>;
}
