/** Vendor marks from @lobehub/icons; see public/logos/README.md. */
import { cn } from "../../lib/cn";

export type BackendKind = "claude" | "codex" | "opencode" | "unknown";
export function normalizeBackend(name: string): BackendKind {
  const n = name.toLowerCase();
  if (n.includes("claude")) return "claude";
  if (n.includes("codex")) return "codex";
  if (n.includes("opencode")) return "opencode";
  return "unknown";
}
export function BackendLogo({ backend, size = 7 }: { backend: string; size?: 6 | 7 | 8 }) {
  const kind = normalizeBackend(backend);
  const file = kind === "opencode" ? "opencode" : `${kind}-color`;
  return <span aria-hidden className={cn("flex shrink-0 items-center justify-center rounded-lg", size === 6 ? "size-6" : size === 8 ? "size-8" : "size-7", kind === "codex" ? "bg-white" : "bg-surface-2")}>
    {kind !== "unknown" ? <img src={`/logos/${file}.svg`} alt="" className={cn("size-5", kind === "opencode" && "invert")} /> : <span className="size-2 rounded-full bg-ink-muted" />}
  </span>;
}
