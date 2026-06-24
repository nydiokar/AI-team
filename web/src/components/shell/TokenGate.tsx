/**
 * TokenGate — collects the Bearer DASHBOARD_TOKEN before any /api/* call
 * (dashboard._require_auth). A focused, centered panel with the product mark.
 */
import { useState } from "react";
import { Terminal, ArrowRight } from "lucide-react";
import { useAuthStore } from "../../stores/authStore";
import { Button } from "../ui/Button";

export function TokenGate() {
  const setToken = useAuthStore((s) => s.setToken);
  const [value, setValue] = useState("");
  const submit = () => value.trim() && setToken(value);

  return (
    <div className="flex h-full flex-col items-center justify-center px-6">
      <div className="card-elev w-full max-w-sm rounded-xl p-6 text-center">
        <div className="mx-auto mb-4 flex size-12 items-center justify-center rounded-xl bg-accent-dim/50 text-accent">
          <Terminal className="size-6" />
        </div>
        <h1 className="text-lg font-semibold tracking-tight text-ink">AI-Team Gateway</h1>
        <p className="mt-1 text-sm text-ink-soft">
          Enter your <code className="font-mono text-accent">DASHBOARD_TOKEN</code> to connect.
        </p>
        <input
          type="password"
          value={value}
          autoFocus
          aria-label="Dashboard token"
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
          placeholder="DASHBOARD_TOKEN"
          className="mt-5 h-11 w-full rounded-full border border-hairline bg-base px-4 text-sm text-ink outline-none transition-colors placeholder:text-ink-muted focus:border-accent/50 focus:ring-2 focus:ring-accent/20"
        />
        <Button onClick={submit} disabled={!value.trim()} className="mt-3 w-full">
          Connect <ArrowRight className="size-4" />
        </Button>
      </div>
      <p className="mt-4 max-w-sm text-center text-xs text-ink-muted">
        Read-only in this build. The dashboard runs at{" "}
        <code className="font-mono">:9003</code>.
      </p>
    </div>
  );
}
