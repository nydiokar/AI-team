import { useEffect, useRef, useState, type FormEvent } from "react";
import { api } from "../../transport/apiClient";
import { useAuthStore } from "../../stores/authStore";
import type { Target } from "../../domain/models";

interface Props {
  target: Target;
}

function renderTerminalOutput(output: string) {
  return output.split(/(https:\/\/[^\s]+)/g).map((part, index) =>
    part.startsWith("https://") ? (
      <a key={index} href={part} target="_blank" rel="noopener noreferrer" className="break-all text-accent underline">
        {part}
      </a>
    ) : part,
  );
}

export function ClaudeLoginPanel({ target }: Props) {
  const token = useAuthStore((state) => state.token);
  const [loginId, setLoginId] = useState("");
  const [status, setStatus] = useState("idle");
  const [output, setOutput] = useState("");
  const [input, setInput] = useState("");
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);
  const offset = useRef(0);
  const terminal = useRef<HTMLPreElement>(null);

  useEffect(() => {
    terminal.current?.scrollTo(0, terminal.current.scrollHeight);
  }, [output]);

  useEffect(() => {
    if (!loginId) return;
    let active = true;
    const poll = async () => {
      try {
        const result = await api.pollClaudeAuth(token, target.id, loginId, offset.current);
        if (!active) return;
        setError("");
        if (result.output) setOutput((current) => (current + result.output).slice(-65536));
        offset.current = result.offset ?? offset.current;
        setStatus(
          result.status === "exited"
            ? result.authenticated ? "authenticated" : "not_authenticated"
            : result.status,
        );
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : "Login connection failed");
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 700);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [loginId, target.id, token]);

  useEffect(() => () => {
    if (loginId) void api.cancelClaudeAuth(token, target.id, loginId).catch(() => undefined);
  }, [loginId, target.id, token]);

  const start = async () => {
    setError("");
    setOutput("");
    offset.current = 0;
    setStarting(true);
    try {
      const result = await api.startClaudeAuth(token, target.id);
      setLoginId(result.login_id);
      setStatus("running");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start Claude login");
    } finally {
      setStarting(false);
    }
  };

  const send = async (event: FormEvent) => {
    event.preventDefault();
    if (!loginId) return;
    setError("");
    try {
      await api.sendClaudeAuthInput(token, target.id, loginId, `${input}\n`);
      setInput("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not send login input");
    }
  };

  const cancel = async () => {
    if (!loginId) return;
    try {
      await api.cancelClaudeAuth(token, target.id, loginId);
    } catch {
      // The login may already have exited; clearing the UI remains safe.
    }
    setLoginId("");
    setStatus("idle");
  };

  return (
    <section className="mt-4 border-t border-hairline pt-4" aria-label="Claude Code login">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-ink">Claude Code login</h3>
          <p className="text-[11px] text-ink-muted">Open the login link here, then paste the returned code into this panel.</p>
        </div>
        {!loginId ? (
          <button onClick={() => void start()} disabled={!target.live || !target.backends.includes("claude") || starting}
            className="rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50">
            Start login
          </button>
        ) : (
          <button onClick={() => void cancel()} className="rounded-lg border border-hairline px-3 py-1.5 text-xs text-ink-soft">
            {status === "running" ? "Cancel" : "Done"}
          </button>
        )}
      </div>
      {status !== "idle" && <p className="mb-2 text-[11px] text-ink-muted">
        {status === "authenticated" ? "Claude is signed in." : status === "not_authenticated" ? "Login ended, but Claude is still signed out. Review the prompt above and try again." : `Login ${status}.`}
      </p>}
      <pre ref={terminal} className="max-h-52 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-surface-2 p-3 font-mono text-[11px] leading-relaxed text-ink" aria-live="polite">
        {output ? renderTerminalOutput(output) : "The login prompt will appear here."}
      </pre>
      {loginId && status === "running" && (
        <form onSubmit={(event) => void send(event)} className="mt-2 flex gap-2">
          <input value={input} onChange={(event) => setInput(event.target.value)} maxLength={4000}
            aria-label="Claude login response" placeholder="Paste code; leave blank to press Enter"
            className="min-w-0 flex-1 rounded-lg border border-hairline bg-surface-1 px-3 py-2 text-xs text-ink" />
          <button type="submit" className="rounded-lg border border-hairline px-3 py-2 text-xs text-ink-soft">Send</button>
        </form>
      )}
      {error && <p className="mt-2 text-xs text-bad" role="alert">{error}</p>}
    </section>
  );
}
