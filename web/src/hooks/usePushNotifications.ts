/**
 * Web Push (#21) subscription lifecycle for the PWA.
 *
 * Rules that mirror the backend dispatch:
 * - Permission is requested ONLY on an explicit user gesture (subscribe()).
 *   Never auto-prompt on load — we merely read current state.
 * - Push is best-effort: if the service worker, PushManager, VAPID config, or
 *   permission is missing, we report a clear state and do nothing destructive.
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "../transport/apiClient";
import { useAuthStore } from "../stores/authStore";

export type PushState =
  | "unsupported" // browser lacks SW/PushManager/Notification
  | "unavailable" // backend has no VAPID configured
  | "denied" // user blocked notifications
  | "default" // supported + available, not yet subscribed
  | "subscribed"
  | "loading";

function unavailableMessage(reason: string | null, missingEnv: string[] | undefined): string {
  if (reason === "pywebpush_not_installed") {
    return "Push delivery is not installed on this gateway.";
  }
  if (reason === "vapid_not_configured" && missingEnv?.length) {
    return `Gateway push setup is incomplete (${missingEnv.join(", ")}).`;
  }
  if (reason === "vapid_public_key_malformed") {
    return "Gateway push setup has an invalid public key.";
  }
  if (reason === "db_unavailable") {
    return "Push subscriptions are temporarily unavailable.";
  }
  return "Push notifications are unavailable on this gateway.";
}

function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  const buffer = new ArrayBuffer(raw.length);
  const out = new Uint8Array(buffer);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

const supported =
  typeof navigator !== "undefined" &&
  "serviceWorker" in navigator &&
  typeof window !== "undefined" &&
  "PushManager" in window &&
  "Notification" in window;

export function usePushNotifications() {
  const token = useAuthStore((s) => s.token);
  const [state, setState] = useState<PushState>("loading");
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!supported) {
      setState("unsupported");
      return;
    }
    try {
      const status = await api.pushStatus(token);
      if (!status.available) {
        setError(unavailableMessage(status.reason, status.missing_env));
        setState("unavailable");
        return;
      }
      setError(null);
      if (Notification.permission === "denied") {
        setState("denied");
        return;
      }
      const reg = await navigator.serviceWorker.ready;
      const existing = await reg.pushManager.getSubscription();
      setState(existing ? "subscribed" : "default");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setState("unavailable");
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const subscribe = useCallback(async () => {
    setError(null);
    try {
      const status = await api.pushStatus(token);
      if (!status.available || !status.vapid_public_key) {
        setError(unavailableMessage(status.reason, status.missing_env));
        setState("unavailable");
        return;
      }
      // Permission prompt happens here — on the user's click, never on load.
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        setState(permission === "denied" ? "denied" : "default");
        return;
      }
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(status.vapid_public_key),
      });
      await api.pushSubscribe(token, sub.toJSON(), navigator.userAgent.slice(0, 80));
      setState("subscribed");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [token]);

  const unsubscribe = useCallback(async () => {
    setError(null);
    try {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      if (sub) {
        await api.pushUnsubscribe(token, sub.endpoint).catch(() => {});
        await sub.unsubscribe();
      }
      setState("default");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [token]);

  return { state, error, subscribe, unsubscribe, refresh };
}
