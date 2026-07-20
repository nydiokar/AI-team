import { useState, useRef, useEffect, useLayoutEffect } from "react";
import { ArrowUp, Square, Paperclip } from "lucide-react";
import { Button } from "../ui/Button";
import { ApiError, newIdempotencyKey } from "../../transport/apiClient";
import {
  useSubmitInstruction,
  useStopSession,
  useUploadFile,
} from "../../hooks/useSessionActions";
import { useSentStore } from "../../stores/sentStore";
import { useDraftStore } from "../../stores/draftStore";
import { useForkStore } from "../../stores/forkStore";

export function Composer({
  sessionId,
  running,
}: {
  sessionId: string;
  running: boolean;
}) {
  // Seed from any persisted draft for this session so a half-typed instruction
  // survives navigating away and back (and full reloads / PWA restarts).
  const setDraft = useDraftStore((s) => s.setDraft);
  const clearDraft = useDraftStore((s) => s.clearDraft);
  const [text, setText] = useState(
    () => useDraftStore.getState().bySession[sessionId] ?? "",
  );
  const [uploadBanner, setUploadBanner] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // When the Composer is reused for a different session (route param change
  // without a remount), swap in that session's draft instead of leaking this
  // session's text into it.
  const lastSessionRef = useRef(sessionId);
  useEffect(() => {
    if (lastSessionRef.current !== sessionId) {
      lastSessionRef.current = sessionId;
      setText(useDraftStore.getState().bySession[sessionId] ?? "");
    }
  }, [sessionId]);

  // Single writer for the input: keeps the visible text and the persisted draft
  // in lockstep so every code path (typing, send, upload, error-restore) stays
  // consistent without repeating the store call.
  const updateText = (value: string) => {
    setText(value);
    setDraft(sessionId, value);
  };

  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = el.scrollHeight + "px";
  }, [text]);

  const submit = useSubmitInstruction();
  const stop = useStopSession();
  const upload = useUploadFile();
  const addSent = useSentStore((s) => s.add);
  const updateSent = useSentStore((s) => s.update);
  // [Session-fork] Any carry-over stashed for THIS (forked) session rides in on the
  // first send only. Read on demand at send time so a stale render never re-attaches
  // it, and clear on success so subsequent turns are normal.
  const clearCarry = useForkStore((s) => s.clearCarry);

  const send = (overrideText?: string) => {
    const body = (overrideText ?? text).trim();
    if (!body || submit.isPending) return;
    const id = newIdempotencyKey();
    const carry = useForkStore.getState().bySession[sessionId];
    addSent({
      id,
      sessionId,
      text: body,
      createdAt: new Date().toISOString(),
      delivery: "sending",
      taskId: null,
    });
    // Sent → the draft's job is done. Clear both the input and the persisted
    // draft; restore them together if the submit is rejected.
    setText("");
    clearDraft(sessionId);
    submit.mutate(
      {
        description: body,
        sessionId,
        idempotencyKey: id,
        continueInline: carry?.continueInline,
        caseId: carry?.caseId || undefined,
      },
      {
        onSuccess: (res) => {
          updateSent(id, { delivery: "acknowledged", taskId: res.task_id });
          // The fork context has been delivered — never attach it again.
          if (carry) clearCarry(sessionId);
        },
        onError: () => {
          updateSent(id, { delivery: "rejected" });
          updateText(body);
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
            updateText("");
            send(instruction);
          } else {
            // No instruction yet — show a transient banner with the saved path.
            // We do NOT inject a fake chat bubble: it isn't a backend event, so it
            // would refresh away and sort out of order. The path is auto-appended
            // to the next instruction the user sends (see fileRef above).
            setUploadBanner(
              `Saved ${res.filename} (${Math.round(res.size / 1024)} KB) → ${res.path}. Type an instruction to use it.`,
            );
            setTimeout(() => setUploadBanner(null), 8000);
          }
        },
        onError: (err) => {
          setUploadBanner(null);
          updateText(`Upload failed: ${String(err.message)}. `);
        },
      },
    );
  };

  const rejected = submit.isError;
  // A 409 from the Level-3 admission gate is terminal for this send — retrying
  // will not help; it needs operator approval. Surface the backend's human copy
  // instead of the generic "tap send to retry" (which would be misleading here).
  const blockedMessage =
    submit.error instanceof ApiError && submit.error.status === 409
      ? submit.error.message
      : null;

  const sendOnEnter = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div
      className="border-t border-hairline bg-surface-1/95 px-3 py-2.5 backdrop-blur-xl"
      style={{ paddingBottom: "max(0.625rem, env(safe-area-inset-bottom))" }}
    >
      {uploadBanner && (
        <p className="mb-1.5 px-1 text-[11px] text-ink-soft">{uploadBanner}</p>
      )}
      {rejected && !uploadBanner && (
        <p className="mb-1.5 px-1 text-[11px] text-bad">
          {blockedMessage ?? "Send failed — tap send to retry."}
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
        <textarea
          ref={textareaRef}
          value={text}
          aria-label="Instruction text"
          onChange={(e) => updateText(e.target.value)}
          onKeyDown={sendOnEnter}
          placeholder={running ? "Task running…" : "Send an instruction…"}
          rows={1}
          className="min-h-[44px] max-h-[160px] flex-1 resize-none overflow-y-auto rounded-2xl bg-surface-2 px-4 py-3 text-[15px] text-ink outline-none ring-1 ring-inset ring-transparent transition-shadow placeholder:text-ink-muted focus:ring-accent/50"
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
