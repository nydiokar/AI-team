import { useId, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { Camera, Upload, X } from "lucide-react";

const storageKey = (id: string) => `ai-team:node-avatar:${id}`;
function savedAvatar(id: string): string | null {
  try {
    const value = localStorage.getItem(storageKey(id));
    return value && value.length <= 50_000 && /^data:image\/jpeg;base64,/.test(value) ? value : null;
  } catch { return null; }
}

/** Deterministic, local identity: no external avatar service or tracking. */
export function NodeAvatar({ id, health }: { id: string; health: string }) {
  const [src, setSrc] = useState(() => savedAvatar(id));
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const inputId = useId();
  const hash = [...id].reduce((h, c) => (Math.imul(h, 31) + c.charCodeAt(0)) | 0, 7) >>> 0;
  const hue = hash % 360;
  const picture = <div className="flex size-12 items-center justify-center overflow-hidden rounded-2xl ring-1 ring-white/10" style={{ background: `linear-gradient(145deg, hsl(${hue} 45% 32%), hsl(${(hue + 45) % 360} 38% 15%))` }}>
    {src ? <img src={src} alt="" className="size-full object-cover" /> : <svg viewBox="0 0 40 40" className="size-9" aria-hidden>
      {Array.from({ length: 25 }, (_, i) => {
        const x = i % 5, y = Math.floor(i / 5), bit = y * 3 + Math.min(x, 4 - x);
        return hash & (1 << bit) ? <rect key={i} x={5 + x * 6} y={5 + y * 6} width="5" height="5" rx="1.2" fill={`hsl(${hue} 65% 82%)`} /> : null;
      })}
    </svg>}
  </div>;

  async function upload(file?: File) {
    if (!file) return;
    setError("");
    if (!["image/jpeg", "image/png", "image/webp"].includes(file.type) || file.size > 2 * 1024 * 1024) {
      setError("Choose a PNG, JPG, or WebP image under 2 MB."); return;
    }
    setBusy(true);
    try {
      const bitmap = await createImageBitmap(file);
      try {
        if (bitmap.width * bitmap.height > 24_000_000) throw new Error("Image dimensions are too large. Choose an image under 24 megapixels.");
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = 128;
        const ctx = canvas.getContext("2d");
        if (!ctx) throw new Error("Image processing is unavailable in this browser.");
        const side = Math.min(bitmap.width, bitmap.height);
        ctx.drawImage(bitmap, (bitmap.width - side) / 2, (bitmap.height - side) / 2, side, side, 0, 0, 128, 128);
        const next = canvas.toDataURL("image/jpeg", 0.85);
        if (next.length > 50_000) throw new Error("This image could not be resized. Try another picture.");
        localStorage.setItem(storageKey(id), next);
        setSrc(next);
        setOpen(false);
      } finally { bitmap.close(); }
    } catch (err) {
      setError(err instanceof Error && err.message.includes("image") ? err.message : "Could not save this picture. Check the image and browser storage permissions.");
    } finally { setBusy(false); }
  }

  return <Dialog.Root open={open} onOpenChange={setOpen}>
    <Dialog.Trigger asChild>
      <button type="button" aria-label={`Change ${id} picture`} className="group relative shrink-0 rounded-2xl focus-visible:outline-2 focus-visible:outline-accent">
        {picture}
        <span role="img" aria-label={health === "online" ? "Node online" : health === "offline" ? "Node offline" : "Node status unknown"} className={`absolute -bottom-0.5 -right-0.5 size-3.5 rounded-full border-[3px] border-surface-1 ${health === "online" ? "bg-ok" : health === "offline" ? "bg-bad" : "bg-ink-muted"}`} />
        <span className="absolute inset-0 flex items-center justify-center rounded-2xl bg-black/50 opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100"><Camera className="size-5 text-white" /></span>
      </button>
    </Dialog.Trigger>
    <Dialog.Portal>
      <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60" />
      <Dialog.Content className="card-elev fixed bottom-0 left-1/2 z-50 w-full max-w-[480px] -translate-x-1/2 rounded-t-3xl p-5 pb-8">
        <div className="mb-3 flex items-center justify-between gap-3">
          <Dialog.Title className="truncate text-base font-semibold text-ink">{id} · picture</Dialog.Title>
          <Dialog.Close aria-label="Close picture settings" className="flex size-11 items-center justify-center rounded-full text-ink-soft"><X className="size-5" /></Dialog.Close>
        </div>
        <Dialog.Description className="mb-5 text-sm text-ink-muted">Choose a square picture, or we’ll crop the center. Saved on this browser only. PNG, JPG, or WebP · up to 2 MB.</Dialog.Description>
        <div className="flex items-center gap-4">
          {picture}
          <label htmlFor={inputId} className="flex min-h-11 cursor-pointer items-center gap-2 rounded-xl bg-accent-dim px-4 text-sm font-medium text-accent focus-within:outline-2 focus-within:outline-accent"><Upload className="size-4" />{busy ? "Saving…" : "Choose picture"}
            <input id={inputId} type="file" accept="image/png,image/jpeg,image/webp" disabled={busy} className="sr-only" onChange={(event) => { void upload(event.target.files?.[0]); event.target.value = ""; }} />
          </label>
        </div>
        {src && <button type="button" disabled={busy} onClick={() => {
          try { localStorage.removeItem(storageKey(id)); setSrc(null); setError(""); }
          catch { setError("Browser storage is unavailable."); }
        }} className="mt-4 min-h-11 text-sm text-ink-soft">Use default identity</button>}
        {error && <p role="alert" className="mt-3 text-sm text-bad">{error}</p>}
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>;
}
