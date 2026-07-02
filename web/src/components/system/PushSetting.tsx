/**
 * Quiet notifications control (#21). Renders in the System → Settings footnote.
 * Only shows an actionable button when push is genuinely available; otherwise it
 * states the honest reason (unsupported / unavailable / blocked) and does nothing.
 */
import { Bell, BellOff } from "lucide-react";
import { Button } from "../ui/Button";
import { usePushNotifications } from "../../hooks/usePushNotifications";

export function PushSetting() {
  const { state, error, subscribe, unsubscribe } = usePushNotifications();

  // Nothing to offer — stay quiet rather than showing a dead control.
  if (state === "unsupported" || state === "unavailable" || state === "loading") {
    return null;
  }

  let body: React.ReactNode;
  let action: React.ReactNode = null;

  if (state === "denied") {
    body = "Notifications are blocked in your browser settings.";
  } else if (state === "subscribed") {
    body = "Push notifications on for task completions.";
    action = (
      <Button size="sm" variant="ghost" onClick={() => void unsubscribe()}>
        Turn off
      </Button>
    );
  } else {
    body = "Get a push when a task finishes or fails.";
    action = (
      <Button size="sm" onClick={() => void subscribe()}>
        Enable
      </Button>
    );
  }

  const Icon = state === "subscribed" ? Bell : BellOff;

  return (
    <div className="card-elev flex items-center gap-3 rounded-xl px-4 py-3.5">
      <Icon className="size-4 shrink-0 text-accent" />
      <div className="min-w-0 flex-1">
        <p className="text-[13px] text-ink-soft">{body}</p>
        {error && <p className="mt-0.5 text-[11px] text-bad">{error}</p>}
      </div>
      {action}
    </div>
  );
}
