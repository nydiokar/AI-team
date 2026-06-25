import { useState, useRef } from "react";
import { ArrowUp, Square, Paperclip } from "lucide-react";
import { Button } from "../ui/Button";
import { newIdempotencyKey } from "../../transport/apiClient";
import {
  useSubmitInstruction,
  useStopSession,
  useUploadFile,
} from "../../hooks/useSessionActions";
import { useSentStore } from "../../stores/sentStore";

export function Composer({
  sessionId,
  running,
}: {
  sessionId: string;
  running: boolean;
}) {
  const [text, setText] = useState("");
  const [uploadBanner, setUploadBanner] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const submit = useSubmitInstruction();
  const stop = useStopSession();
  const upload = useUploadFile();
  const addSent = useSentStore((s) => s.add);
  const updateSent = useSentStore((s) => s.update);

  const send = (overrideText?: string) => {
    const body = (overrideText ?? text).trim();
    if (!body || submit.isPending) return;
    const id = newIdempotencyKey();
    addSent({
      id,
      sessionId,
      text: body,
      createdAt: new Date().toISOString(),
      delivery: "sending",
      taskId: null,
    });
    setText("");
    submit.mutate(
      { description: body, sessionId, idempotencyKey: id },
      {
        onSuccess: (res) =>
          updateSent(id, { delivery: "acknowledged", taskId: res.task_id }),
        onError: () => {
          updateSent(id, { delivery: "rejected" });
          setText(body);
        },
      },
    );
  };

  const handleFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.value = "";
    setUploadBanner(`Uploading ${file.name}…`);
    upload.mutate(
      { sessionId, file },
      {
        onSuccess: (res) => {
          setUploadBanner(null);
          const fileRef = `📎 File: ${res.path}`;
          const instruction = text.trim() ? `${text.trim()}\n\n${fileRef}` : null;
          if (instruction) {
            setText("");
            send(instruction);
          } else {
            setUploadBanner(
              `Saved ${res.filename} (${Math.round(res.size / 1024)} KB). Type an instruction to work with it.`,
            );
            setTimeout(() => setUploadBanner(null), 5000);
          }
        },
        onError: (err) => {
          setUploadBanner(null);
          setText(`Upload failed: ${String(err.message)}. `);
        },
      },
    );
  };

  const rejected = submit.isError;

  return (
    <div
      className="border-t border-hairline bg-surface-1/95 px-3 py-2.5 backdrop-blur-xl"
      style={{ paddingBottom: `max(0.625rem, env(safe-area-inset-bottom))` }}
    >
      {uploadBanner && (
        <p className="mb-1.5 px-1 text-[11px] text-ink-soft">{uploadBanner}</p>
      )}
      {rejected && !uploadBanner && (
        <p className="mb-1.5 px-1 text-[11px] text-bad">
          Send failed — tap send to retry.
        </p>
      )}
      <div className="flex items-end gap-2">
        {running ? (
          <button
            onClick={() => stop.mutate(sessionId)}
            disabled={stop.isPending}
            className="flex size-11 shrink-0 items-center justify-center rounded-full border border-bad/50 text-bad hover:bg-bad/10 disabled:opacity-50"
            aria-label="Stop running task"
          >
            <Square className="size-4" fill="currentColor" />
          </button>
        ) : (
          <>
            <input
              ref={fileInputRef}
              type="file"
              className="hidden"
              onChange={handleFile}
              aria-hidden
            />
            <button
              onClick={() => fileInputRef.current?.click()}
              disabled={upload.isPending}
              className="flex size-11 shrink-0 items-center justify-center rounded-full border border-hairline text-ink-muted hover:bg-surface-2 disabled:opacity-50"
              aria-label="Upload file"
            >
              <Paperclip className="size-5" />
            </button>
          </>
        )}
        <input
          value={text}
          aria-label="Instruction text"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          placeholder={running ? "Task running…" : "Send an instruction…"}
          className="h-11 flex-1 rounded-full border border-hairline bg-base px-4 text-sm text-ink outline-none placeholder:text-ink-muted focus:border-accent/50"
        />
        <Button
          size="icon"
          aria-label="Send"
          disabled={!text.trim() || submit.isPending}
          onClick={() => send()}
        >
          <ArrowUp className="size-5" />
        </Button>
      </div>
    </div>
  );
}
