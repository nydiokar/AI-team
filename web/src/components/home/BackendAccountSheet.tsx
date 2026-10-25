import { useEffect, useId, useMemo, useRef, useState, type FormEvent } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { Check, Copy, ExternalLink, LoaderCircle } from "lucide-react";
import { api } from "../../transport/apiClient";
import { useAuthStore } from "../../stores/authStore";
import { copyTextToClipboard } from "../../lib/clipboard";
import { useBackendAccountStatus } from "../../hooks/useBackendAccountStatus";
import type { Target } from "../../domain/models";

export type AccountBackend = "claude" | "codex";

export function safeAuthUrl(value: string, backend: AccountBackend): boolean {
  try {
    const url = new URL(value);
    const hosts = backend === "claude" ? ["claude.com", "claude.ai", "console.anthropic.com", "platform.claude.com"] : ["auth.openai.com"];
    return url.protocol === "https:" && !url.username && !url.password && (!url.port || url.port === "443") && hosts.includes(url.hostname);
  } catch { return false; }
}

function loginOutput(output: string, backend: AccountBackend) {
  const clean = output.replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, "").replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "");
  const urls = clean.match(/https:\/\/[^\s\x1b<>"']+/g) ?? [];
  return {
    url: urls.reverse().find((url) => safeAuthUrl(url, backend)),
    details: clean.replace(/https:\/\/[^\s\x1b<>"']+/g, "[link hidden]"),
    deviceCode: backend === "codex" ? clean.match(/\b[A-Z0-9]{4}-[A-Z0-9]{4,5}\b/)?.[0] : undefined,
  };
}

function loginKey(nodeId: string, backend: AccountBackend): string {
  return `backend-login:${backend}:${nodeId}`;
}

function rememberedLogin(key: string): string {
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) ?? "null");
    return saved && /^[a-f0-9]{32}$/.test(saved.id) && saved.expires > Date.now() ? saved.id : "";
  } catch { return ""; }
}

function rememberLogin(key: string, id: string): void {
  try {
    if (id) sessionStorage.setItem(key, JSON.stringify({ id, expires: Date.now() + 600_000 }));
    else sessionStorage.removeItem(key);
  } catch { /* The worker still owns the bounded login if storage is unavailable. */ }
}

function loginError(error: unknown): string {
  const reason = error instanceof Error ? error.message : "unknown_error";
  const messages: Record<string, string> = {
    worker_unreachable: "The gateway cannot reach this node's login service. Check its connection and worker port 9001. Your login may still be running.",
    worker_auth_mismatch: "The node rejected the gateway credential. The worker and gateway mesh tokens must match.",
    worker_auth_route_unavailable: "This worker does not support the login route. Update the worker before signing in.",
    login_in_progress: "A login is already running on this node. Reopen its account panel to resume it, or wait for its ten-minute expiry.",
    login_not_found: "The node no longer has this login (it may have restarted or expired). Start a new login.",
    login_expired: "This login expired after ten minutes. Start a new login.",
    login_finished: "This login has ended. Refresh account status before starting again.",
    backend_missing: "The provider CLI is not installed on this node.",
    node_offline: "This node is offline. Reconnect it before signing in.",
    worker_rejected_request: "The worker rejected this login request. Check the gateway log for the node and action.",
    login_input_timeout: "The node did not accept the code in time. Your code is still here; try sending it again.",
  };
  return `${messages[reason] ?? "Login request failed. Retry when the node is available."} (${reason})`;
}

function NodeAccount({ node, backend }: { node: Target; backend: AccountBackend }) {
  const token = useAuthStore((s) => s.token);
  const storageKey = loginKey(node.id, backend);
  const [loginId, setLoginId] = useState(() => rememberedLogin(storageKey));
  const [running, setRunning] = useState(() => Boolean(rememberedLogin(storageKey)));
  const [busy, setBusy] = useState(false);
  const account = useBackendAccountStatus(token, node.id, backend, !running && !busy);
  const state = account.isError ? "unavailable" : account.data?.state ?? (account.isPending ? "loading" : "unavailable");
  const [output, setOutput] = useState("");
  const [input, setInput] = useState("");
  const [inputSupported, setInputSupported] = useState(backend === "claude");
  const [sending, setSending] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState("");
  const [error, setError] = useState("");
  const inputId = useId();
  const sendingRef = useRef(false);
  const activeLogin = useRef(loginId);
  const mounted = useRef(false);
  const operation = useRef(0);
  const { url, details, deviceCode } = useMemo(() => loginOutput(output, backend), [output, backend]);

  useEffect(() => { setCopied(false); setCopyError(""); }, [url]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      operation.current++;
      // Closing/minimizing detaches the UI; only Cancel login ends the worker process.
    };
  }, [token, node.id, backend]);

  useEffect(() => {
    if (!loginId || !running) return;
    let active = true;
    const current = operation.current;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let offset = 0;
    let failures = 0;
    setOutput("");
    const poll = async () => {
      try {
        const result = await api.backendAuth(token, node.id, backend, "poll", { login_id: loginId, offset }, controller.signal);
        if (!active || current !== operation.current) return;
        failures = 0;
        setError("");
        offset = result.offset ?? offset;
        if (result.output) setOutput((old) => (old + result.output).slice(-65536));
        if (typeof result.input_supported === "boolean") setInputSupported(result.input_supported);
        if (result.status === "exited") {
          activeLogin.current = "";
          rememberLogin(storageKey, "");
          setRunning(false);
          account.record({
            state: result.authenticated === true ? "signed_in" : result.authenticated === false ? "signed_out" : "unavailable",
            checked_at: result.checked_at ?? new Date().toISOString(),
            login_verified_at: result.login_verified_at ?? (result.authenticated === true ? new Date().toISOString() : null),
          });
          setInput("");
          setError(result.authenticated === true ? "" : result.authenticated === false ? "Login was not completed. Start a new login to try again." : "Login ended, but the node could not verify the account. Try checking again.");
          return;
        }
        timer = setTimeout(() => void poll(), 700);
      } catch (error) {
        if (active && current === operation.current) {
          const reason = error instanceof Error ? error.message : "";
          if (["login_not_found", "login_expired", "backend_mismatch", "worker_auth_route_unavailable", "worker_auth_mismatch"].includes(reason)) {
            activeLogin.current = "";
            rememberLogin(storageKey, "");
            setLoginId(""); setRunning(false);
            setError(loginError(error));
            account.record({ state: "unavailable" });
          } else {
            setError(`Reconnecting to ${node.id}… ${loginError(error)}`);
            timer = setTimeout(() => void poll(), Math.min(5000, 1000 * ++failures));
          }
        }
      }
    };
    void poll();
    return () => { active = false; clearTimeout(timer); controller.abort(); };
  }, [loginId, running, token, node.id, backend]);

  const start = async () => {
    const current = ++operation.current;
    setBusy(true); setError(""); setOutput(""); setInput(""); setSubmitted(false);
    setCopied(false); setCopyError("");
    try {
      await account.cancel();
      const result = await api.backendAuth(token, node.id, backend, "start");
      rememberLogin(storageKey, result.login_id);
      if (!mounted.current || current !== operation.current) return;
      activeLogin.current = result.login_id;
      setInputSupported(result.input_supported ?? backend === "claude");
      setLoginId(result.login_id); setRunning(true);
    } catch (error) { if (mounted.current) setError(loginError(error)); }
    finally { if (mounted.current) setBusy(false); }
  };

  const cancel = async () => {
    operation.current++;
    setRunning(false); setBusy(true);
    try {
      await api.backendAuth(token, node.id, backend, "cancel", { login_id: activeLogin.current });
      rememberLogin(storageKey, "");
      activeLogin.current = ""; setLoginId(""); setInput(""); setError("");
    } catch (error) { setError(loginError(error)); setRunning(true); }
    finally { if (mounted.current) setBusy(false); }
  };

  const send = async (event: FormEvent) => {
    event.preventDefault();
    const code = input.trim();
    if (!code || !running || !inputSupported || busy || sendingRef.current) return;
    const current = operation.current;
    sendingRef.current = true;
    setSending(true); setError(""); setSubmitted(false);
    try {
      await api.backendAuth(token, node.id, backend, "input", { login_id: loginId, input: `${code}\n` });
      if (mounted.current && current === operation.current) { setInput(""); setSubmitted(true); }
    } catch (error) { if (mounted.current && current === operation.current) setError(`Could not send code to the node. Your code is still here; try again. ${loginError(error)}`); }
    finally { sendingRef.current = false; if (mounted.current) setSending(false); }
  };

  const copyLink = async () => {
    if (!url) return;
    setCopyError("");
    try {
      await copyTextToClipboard(url);
      if (mounted.current) setCopied(true);
    } catch { if (mounted.current) setCopyError("Could not copy the link. Use Open link to continue."); }
  };

  return <section aria-label={`${node.id} account`} className="space-y-2 border-t border-hairline py-4">
    <div className="flex items-center justify-between gap-3">
      <h3 className="font-semibold text-ink">{node.id}</h3>
      <button disabled={account.isFetching || busy || running} onClick={() => void account.refresh()} className="min-h-11 rounded-lg px-3 text-xs text-ink-muted disabled:opacity-50">{account.isFetching ? "Checking…" : "Refresh status"}</button>
    </div>
    <p className="text-xs text-ink-muted">Credentials are stored on {node.id}.</p>
    <p role="status" className="text-sm text-ink-soft">{running ? submitted ? "Code sent. Waiting for the node to verify your account…" : "Waiting for you to finish signing in…" : state === "signed_in" ? "Signed in" : state === "signed_out" ? "Signed out" : state === "loading" ? "Checking account…" : "Account status unavailable"}</p>
    {account.data?.checked_at && <p className="text-xs text-ink-muted">Status checked <time dateTime={account.data.checked_at}>{new Date(account.data.checked_at).toLocaleString()}</time></p>}
    {account.data?.login_verified_at && <p className="text-xs text-ink-muted">Login verified on {node.id} · <time dateTime={account.data.login_verified_at}>{new Date(account.data.login_verified_at).toLocaleString()}</time></p>}
    {activeLogin.current ? <button disabled={busy} onClick={() => void cancel()} className="rounded-lg border border-hairline px-3 py-2 disabled:opacity-50">Cancel login</button>
      : <button disabled={busy || state === "loading"} onClick={() => void start()} className="min-h-11 rounded-lg bg-accent px-3 py-2 font-medium text-[var(--color-base)] focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50">{busy ? "Starting…" : state === "signed_in" ? "Sign in again" : "Log in"}</button>}
    {loginId && <div className="space-y-4 pt-2">
      {running && <>
        <div className="space-y-3 rounded-xl border border-hairline p-4">
          <div className="space-y-1">
            <h4 className="text-sm font-semibold text-ink">1. Sign in with {backend === "claude" ? "Claude" : "OpenAI"}</h4>
            <p className="text-sm text-ink-muted">Open the secure sign-in page in a new tab. Come back here when you’re done. You can close this panel and reopen it to resume; login expires after ten minutes.</p>
          </div>
          {url ? <div className="grid grid-cols-2 gap-2">
            <button onClick={() => void copyLink()} className="flex min-h-11 items-center justify-center gap-2 rounded-lg border border-hairline px-3 py-2 text-sm font-medium focus-visible:outline-2 focus-visible:outline-accent">
              {copied ? <Check size={16} aria-hidden="true" /> : <Copy size={16} aria-hidden="true" />}{copied ? "Copied" : "Copy link"}
            </button>
            <a href={url} target="_blank" rel="noopener noreferrer" aria-label="Open link in a new tab" className="flex min-h-11 items-center justify-center gap-2 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-[var(--color-base)] focus-visible:outline-2 focus-visible:outline-accent">
              Open link<ExternalLink size={16} aria-hidden="true" />
            </a>
          </div> : <p role="status" className="flex items-center gap-2 text-sm text-ink-muted"><LoaderCircle size={16} className="animate-spin" aria-hidden="true" />Preparing your sign-in link…</p>}
          <span role="status" className="sr-only">{copied ? "Link copied to clipboard" : ""}</span>
          {copyError && <p role="alert" className="text-sm text-bad">{copyError}</p>}
        </div>
        {inputSupported ? <form onSubmit={(e) => void send(e)} className="space-y-3 rounded-xl border border-hairline p-4">
          <div className="space-y-1">
            <label htmlFor={inputId} className="text-sm font-semibold text-ink">2. Paste your authorization code</label>
            <p id={`${inputId}-help`} className="text-sm text-ink-muted">Copy the code from the sign-in page and send it to {node.id} to finish.</p>
          </div>
          <input id={inputId} aria-label={`${backend === "claude" ? "Claude" : "Codex"} login response`} aria-describedby={`${inputId}-help`} value={input} maxLength={1000} disabled={sending || busy} autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false} placeholder="Paste authorization code" onChange={(e) => { setInput(e.target.value); setSubmitted(false); }} className="min-h-11 w-full min-w-0 rounded-lg border border-hairline bg-surface-2 px-3 py-2 text-base text-ink placeholder:text-ink-muted focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50" />
          <button disabled={busy || sending || !input.trim()} type="submit" className="flex min-h-11 w-full items-center justify-center gap-2 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-[var(--color-base)] focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50">
            {sending && <LoaderCircle size={16} className="animate-spin" aria-hidden="true" />}{sending ? "Sending…" : "Send code to node"}
          </button>
        </form> : <div className="space-y-2 rounded-xl border border-hairline p-4">
          <h4 className="text-sm font-semibold text-ink">2. Enter the device code on the sign-in page</h4>
          {deviceCode && <p className="select-all rounded-lg bg-surface-2 p-3 text-center font-mono text-xl tracking-widest text-ink">{deviceCode}</p>}
          <p className="text-sm text-ink-muted">{deviceCode ? "Use this code in the browser. " : "Find your device code in Login details below. "}This page updates automatically when sign-in completes. Your workspace administrator may need to enable device-code authentication.</p>
        </div>}
      </>}
      {details && <details className="text-xs text-ink-muted">
        <summary className="cursor-pointer py-2">Login details</summary>
        <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-surface-2 p-3">{details}</pre>
      </details>}
    </div>}
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
          <Dialog.Title className="text-base font-semibold capitalize text-ink">{backend} accounts</Dialog.Title>
          <Dialog.Close aria-label="Close accounts" className="rounded-lg border border-hairline px-3 py-2">Close</Dialog.Close>
        </div>
        <Dialog.Description className="mb-3 text-sm text-ink-muted">Sign in on each node where you run {backend}. Status is cached for up to five minutes; refresh to check now.</Dialog.Description>
        {eligible.length ? eligible.map((node) => <NodeAccount key={`${backend}:${node.id}`} node={node} backend={backend} />) : <p>No supporting nodes online.</p>}
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>;
}
