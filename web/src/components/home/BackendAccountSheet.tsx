import { useEffect, useId, useMemo, useRef, useState, type FormEvent } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { Check, Copy, ExternalLink, LoaderCircle } from "lucide-react";
import { api } from "../../transport/apiClient";
import { useAuthStore } from "../../stores/authStore";
import { copyTextToClipboard } from "../../lib/clipboard";
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

function NodeAccount({ node, backend }: { node: Target; backend: AccountBackend }) {
  const token = useAuthStore((s) => s.token);
  const [state, setState] = useState("loading");
  const [loginId, setLoginId] = useState("");
  const [running, setRunning] = useState(false);
  const [busy, setBusy] = useState(false);
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
  const activeLogin = useRef("");
  const mounted = useRef(false);
  const operation = useRef(0);
  const { url, details, deviceCode } = useMemo(() => loginOutput(output, backend), [output, backend]);

  useEffect(() => { setCopied(false); setCopyError(""); }, [url]);

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
        if (typeof result.input_supported === "boolean") setInputSupported(result.input_supported);
        if (result.status === "exited") {
          activeLogin.current = "";
          setRunning(false);
          setState(result.authenticated === true ? "signed_in" : result.authenticated === false ? "signed_out" : "unavailable");
          setInput("");
          setError(result.authenticated === true ? "" : result.authenticated === false ? "Login was not completed. Start a new login to try again." : "Login ended, but the node could not verify the account. Try checking again.");
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
    setBusy(true); setError(""); setOutput(""); setInput(""); setSubmitted(false);
    setCopied(false); setCopyError("");
    try {
      const result = await api.backendAuth(token, node.id, backend, "start");
      if (!mounted.current || current !== operation.current) {
        void api.backendAuth(token, node.id, backend, "cancel", { login_id: result.login_id }).catch(() => undefined);
        return;
      }
      activeLogin.current = result.login_id;
      setInputSupported(result.input_supported ?? backend === "claude");
      setLoginId(result.login_id); setRunning(true);
    } catch { if (mounted.current) setError("Could not start login. Check the node and try again."); }
    finally { if (mounted.current) setBusy(false); }
  };

  const cancel = async () => {
    operation.current++;
    setRunning(false); setBusy(true);
    try {
      await api.backendAuth(token, node.id, backend, "cancel", { login_id: activeLogin.current });
      activeLogin.current = ""; setLoginId(""); setInput(""); setError("");
    } catch { setError("Could not cancel login. Try again; it expires after ten minutes."); }
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
    } catch { if (mounted.current && current === operation.current) setError("Could not send code to the node. Your code is still here; try again."); }
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
    <h3 className="font-semibold text-ink">{node.id}</h3>
    <p role="status" className="text-sm text-ink-soft">{running ? submitted ? "Code sent. Waiting for the node to verify your account…" : "Waiting for you to finish signing in…" : state === "signed_in" ? "Signed in" : state === "signed_out" ? "Signed out" : state === "loading" ? "Checking account…" : "Account status unavailable"}</p>
    {activeLogin.current ? <button disabled={busy} onClick={() => void cancel()} className="rounded-lg border border-hairline px-3 py-2 disabled:opacity-50">Cancel login</button>
      : <button disabled={busy || state === "loading"} onClick={() => void start()} className="min-h-11 rounded-lg bg-accent px-3 py-2 font-medium text-[var(--color-base)] focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50">{busy ? "Starting…" : state === "signed_in" ? "Sign in again" : "Log in"}</button>}
    {loginId && <div className="space-y-4 pt-2">
      {running && <>
        <div className="space-y-3 rounded-xl border border-hairline p-4">
          <div className="space-y-1">
            <h4 className="text-sm font-semibold text-ink">1. Sign in with {backend === "claude" ? "Claude" : "OpenAI"}</h4>
            <p className="text-sm text-ink-muted">Open the secure sign-in page in a new tab. Come back here when you’re done.</p>
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
        <Dialog.Description className="mb-3 text-sm text-ink-muted">Accounts on online nodes supporting {backend}.</Dialog.Description>
        {eligible.length ? eligible.map((node) => <NodeAccount key={`${backend}:${node.id}`} node={node} backend={backend} />) : <p>No supporting nodes online.</p>}
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>;
}
