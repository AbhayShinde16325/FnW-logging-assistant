export const NOTIFICATION_POLL_MS = 30000;

export function normalizeNotificationList(payload) {
  if (!payload || !Array.isArray(payload.notifications)) return [];
  return payload.notifications;
}

export function unreadBadgeLabel(count) {
  const value = Number(count) || 0;
  if (value <= 0) return "";
  return value > 99 ? "99+" : String(value);
}

export function notificationDeepLink(search = "") {
  try {
    const value = new URLSearchParams(search).get("task");
    const id = Number(value);
    return Number.isInteger(id) && id > 0 ? id : null;
  } catch {
    return null;
  }
}

export function notificationIdFromSearch(search = "") {
  try {
    const value = new URLSearchParams(search).get("notification");
    const id = Number(value);
    return Number.isInteger(id) && id > 0 ? id : null;
  } catch {
    return null;
  }
}

export function isFollowupNotification(notification) {
  const payload = notification?.payload || {};
  return notification?.notification_type === 'TASK_MISSING_DETAILS' || payload.kind === 'task_missing_details_followup';
}

export function pushPermissionState({ supported, permission, subscribed }) {
  if (!supported) return "unsupported";
  if (permission === "denied") return "denied";
  if (subscribed) return "subscribed";
  if (permission === "granted") return "granted";
  return "prompt";
}

export function base64UrlToUint8Array(value) {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding).replaceAll("-", "+").replaceAll("_", "/");
  const raw = atob(base64);
  const bytes = new Uint8Array(raw.length);

  for (let index = 0; index < raw.length; index += 1) {
    bytes[index] = raw.charCodeAt(index);
  }

  return bytes;
}

export function buildPushSubscriptionPayload(subscription, deviceName = "") {
  const json = subscription?.toJSON ? subscription.toJSON() : subscription;

  return {
    endpoint: json.endpoint,
    keys: {
      p256dh: json.keys?.p256dh || "",
      auth: json.keys?.auth || "",
    },
    device_name: String(deviceName || "").slice(0, 255) || null,
  };
}

export function applyNotificationRead(notifications, notificationId) {
  return notifications.map((item) =>
    item.id === notificationId ? { ...item, is_read: true } : item
  );
}

export function applyAllNotificationsRead(notifications) {
  return notifications.map((item) => ({ ...item, is_read: true }));
}
