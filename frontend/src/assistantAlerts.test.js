import test from "node:test";
import assert from "node:assert/strict";

import {
  announcementText,
  diffNewNotifications,
  normalizePriority,
  shouldAnnounceNotification,
} from "./assistantAlerts.js";

test("priority normalization and announcement behavior", () => {
  assert.equal(normalizePriority("critical"), "CRITICAL");
  assert.equal(normalizePriority("unknown"), "NORMAL");

  const prefs = { attentionEnabled: true, voiceEnabled: true, soundEnabled: true };
  assert.deepEqual(shouldAnnounceNotification({ priority: "LOW" }, prefs), {
    speak: false,
    sound: false,
    priority: "LOW",
  });
  assert.deepEqual(shouldAnnounceNotification({ priority: "NORMAL", notification_type: "TASK_REMINDER" }, prefs), {
    speak: true,
    sound: true,
    priority: "NORMAL",
  });
  assert.deepEqual(shouldAnnounceNotification({ priority: "NORMAL", notification_type: "TASK_COMPLETED" }, prefs), {
    speak: false,
    sound: true,
    priority: "NORMAL",
  });
  assert.deepEqual(shouldAnnounceNotification({ priority: "HIGH" }, prefs), {
    speak: true,
    sound: true,
    priority: "HIGH",
  });
});

test("announcement text prefers voice_text and diffs new notifications", () => {
  assert.equal(
    announcementText({ voice_text: "Speak this", title: "Title", message: "Body" }),
    "Speak this"
  );
  assert.equal(announcementText({ title: "Title", message: "Body" }), "Title. Body");

  const fresh = diffNewNotifications([{ id: 1 }, { id: 2 }, { id: 3 }], new Set([1, 3]));
  assert.deepEqual(fresh.map((item) => item.id), [2]);
});
