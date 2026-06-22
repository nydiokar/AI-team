/**
 * Target selector (spec §7.1) — filters the Sessions list by target. A scrolling
 * row of pill chips, each with a live status dot. Binds to LIVE targets.
 */
import { Boxes } from "lucide-react";
import { useTargets } from "../../hooks/useLiveData";
import { useUiStore } from "../../stores/uiStore";
import { StatusDot } from "../ui/StatusChip";
import { cn } from "../../lib/cn";

export function TargetSelector() {
  const { data: targets } = useTargets();
  const filter = useUiStore((s) => s.targetFilter);
  const setFilter = useUiStore((s) => s.setTargetFilter);

  if (!targets || targets.length === 0) return null;

  const chip = (active: boolean) =>
    cn(
      "flex min-h-9 shrink-0 items-center gap-2 rounded-full border px-3 text-[13px] transition-colors",
      active
        ? "border-accent/40 bg-accent-dim/40 text-accent"
        : "border-hairline bg-surface-1 text-ink-soft hover:text-ink",
    );

  return (
    <div className="flex gap-2 overflow-x-auto px-4 py-2.5 [scrollbar-width:none]">
      <button className={chip(filter === null)} onClick={() => setFilter(null)}>
        <Boxes className="size-3.5" /> All targets
      </button>
      {targets.map((t) => (
        <button key={t.id} className={chip(filter === t.id)} onClick={() => setFilter(t.id)}>
          <StatusDot live={t.live} />
          {t.id}
        </button>
      ))}
    </div>
  );
}
