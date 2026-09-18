/**
 * HostHealthBanner — answers "why is it slow?" in one line, and only when something is
 * wrong. Distinguishes a struggling HOST (disk / memory / heat / CPU) from an app-side
 * cause (gateway event loop, a slow endpoint). Verdict is computed server-side from the
 * gateway's own metrics; nothing renders when healthy or when the gateway predates it.
 */
import { AnimatePresence, motion } from "framer-motion";
import { Activity, Cpu, HardDrive, MemoryStick, Thermometer, TriangleAlert } from "lucide-react";
import { useHostHealth } from "../../hooks/useHostHealth";

const ICON: Record<string, typeof TriangleAlert> = {
  disk: HardDrive,
  memory: MemoryStick,
  thermal: Thermometer,
  cpu: Cpu,
  event_loop: Activity,
};

export function HostHealthBanner() {
  const { data } = useHostHealth();
  const show = data !== undefined && data.status !== "ok";
  const Icon = (data && ICON[data.cause]) || TriangleAlert;
  const tone = data?.status === "bad" ? "text-bad bg-bad/10" : "text-warn bg-warn/10";

  return (
    <AnimatePresence>
      {show && data && (
        <motion.div
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: "auto", opacity: 1 }}
          exit={{ height: 0, opacity: 0 }}
          transition={{ duration: 0.2 }}
          className={`flex items-start justify-center gap-2 overflow-hidden px-4 py-1.5 text-xs ${tone}`}
        >
          <Icon className="mt-0.5 size-3.5 shrink-0" />
          <span>
            <span className="font-medium">{data.headline}</span>
            <span className="opacity-80"> · {data.detail}</span>
          </span>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
