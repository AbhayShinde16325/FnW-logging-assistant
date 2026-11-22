import assert from "node:assert/strict";
import test from "node:test";

import {
  applyAllNotificationsRead,
  applyNotificationRead,
  base64UrlToUint8Array,
  buildPushSubscriptionPayload,
  normalizeNotificationList,
  notificationDeepLink,
  notificationIdFromSearch,
  pushPermissionState,
  unreadBadgeLabel,
} from "./notifications.js";

test("notification lists tolerate empty and malformed payloads", () => {
  assert.deepEqual(normalizeNotificationList(null), []);
  assert.deepEqual(normalizeNotificationList({ notifications: null }), []);
  assert.deepEqual(
    normalizeNotificationList({ notifications: [{ id: 1 }] }),
    [{ id: 1 }]
  );
});

test("unread badge labels are bounded", () => {
  assert.equal(unreadBadgeLabel(0), "");
  assert.equal(unreadBadgeLabel(4), "4");
  assert.equal(unreadBadgeLabel(120), "99+");
});

test("deep links and notification ids are parsed safely", () => {
  assert.equal(notificationDeepLink("?task=42&notification=9"), 42);
  assert.equal(notificationIdFromSearch("?task=42&notification=9"), 9);
  assert.equal(notificationDeepLink("?task=bad"), null);
});

test("push permission state distinguishes unsupported and denied", () => {
  assert.equal(
    pushPermissionState({ supported: false, permission: "granted" }),
    "unsupported"
  );
  assert.equal(
    pushPermissionState({ supported: true, permission: "denied" }),
    "denied"
  );
  assert.equal(
    pushPermissionState({
      supported: true,
      permission: "granted",
      subscribed: true,
    }),
    "subscribed"
  );
});

test("VAPID public keys are decoded without padding", () => {
  const bytes = base64UrlToUint8Array("AQID");
  assert.deepEqual([...bytes], [1, 2, 3]);
});

test("push subscription payload contains only safe fields", () => {
  const payload = buildPushSubscriptionPayload(
    {
      endpoint: "https://push.example.com/one",
      keys: { p256dh: "key", auth: "auth" },
    },
    "Chrome desktop"
  );

  assert.deepEqual(payload, {
    endpoint: "https://push.example.com/one",
    keys: { p256dh: "key", auth: "auth" },
    device_name: "Chrome desktop",
  });
});

test("read helpers update only the intended notification", () => {
  const rows = [
    { id: 1, is_read: false },
    { id: 2, is_read: false },
  ];

  assert.equal(applyNotificationRead(rows, 1)[0].is_read, true);
  assert.equal(applyNotificationRead(rows, 1)[1].is_read, false);
  assert.equal(applyAllNotificationsRead(rows)[1].is_read, true);
});
