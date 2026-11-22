/* Task Flow reminder and push service worker. */

const VERSION = "2026-11-17.1";

self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      try {
        await self.clients.claim();
      } catch {
        // ignore claim failures
      }
    })()
  );
});

function safeText(value, fallback, limit = 300) {
  if (value == null) return fallback;
  const text = String(value).replace(/\s+/g, " ").trim();
  if (!text) return fallback;
  return text.slice(0, limit);
}

function safeTaskUrl(raw) {
  const fallback = self.registration.scope || self.location.origin + "/";

  try {
    const url = new URL(raw || "/", self.location.origin);
    if (url.origin !== self.location.origin) return fallback;
    return url.href;
  } catch {
    return fallback;
  }
}

function buildNotification(data) {
  const priority = safeText(data?.priority || "NORMAL", "NORMAL", 16).toUpperCase();
  const notificationId = data?.notification_id ?? data?.task_id ?? "unknown";
  const requireInteraction =
    data?.requireInteraction !== undefined
      ? Boolean(data.requireInteraction)
      : priority === "HIGH" || priority === "CRITICAL";

  let timestamp = Date.now();
  if (data?.timestamp) {
    const parsed = Number(new Date(data.timestamp));
    if (Number.isFinite(parsed)) timestamp = parsed;
  }

  const options = {
    body: safeText(
      data?.body || data?.message,
      "You have a task notification.",
      500
    ),
    icon: "/favicon.svg",
    badge: "/favicon.svg",
    tag: safeText(
      data?.tag || `task-notification-${notificationId}`,
      `task-notification-${notificationId}`,
      120
    ),
    renotify: Boolean(data?.renotify),
    requireInteraction,
    silent: false,
    timestamp,
    data: {
      url: safeText(
        data?.url || data?.destination_url || (data?.task_id ? `/?task=${data.task_id}` : "/"),
        "/",
        1000
      ),
      task_id: data?.task_id ?? null,
      notification_id: data?.notification_id ?? null,
      notification_type: safeText(
        data?.notification_type || "TASK_REMINDER",
        "TASK_REMINDER",
        64
      ),
      priority,
      voice_text: data?.voice_text ? safeText(data.voice_text, "", 1000) : null,
      deadline: data?.deadline ?? null,
      version: VERSION,
    },
  };

  const title = safeText(data?.title, "Task Flow reminder", 120);
  return { title, options };
}

async function postToClients(message) {
  try {
    const clients = await self.clients.matchAll({
      type: "window",
      includeUncontrolled: true,
    });

    for (const client of clients) {
      try {
        client.postMessage(message);
      } catch {
        // ignore
      }
    }
  } catch {
    // ignore
  }
}

self.addEventListener("push", (event) => {
  event.waitUntil(
    (async () => {
      let data = {};

      try {
        if (event.data) {
          data = event.data.json();
        }
      } catch {
        try {
          data = { body: event.data ? await event.data.text() : "" };
        } catch {
          data = {};
        }
      }

      const { title, options } = buildNotification(data || {});

      try {
        await self.registration.showNotification(title, options);
      } catch {
        // Ensure background delivery still produces a visible notification when
        // a browser rejects some options.
        try {
          await self.registration.showNotification(title || "Task Flow reminder", {
            body: options.body || "You have a task notification.",
            tag: options.tag,
            data: options.data,
          });
        } catch {
          // nothing else we can do in a service worker
          return;
        }
      }

      await postToClients({
        type: "PUSH_RECEIVED",
        task_id: options.data.task_id,
        notification_id: options.data.notification_id,
        notification_type: options.data.notification_type,
        priority: options.data.priority,
        voice_text: options.data.voice_text || null,
        title: title,
      });
    })()
  );
});

self.addEventListener("pushsubscriptionchange", (event) => {
  // We cannot silently re-authenticate as the employee here; keep state clean
  // and let the app re-register on next open.
  event.waitUntil(
    (async () => {
      await postToClients({ type: "PUSH_SUBSCRIPTION_CHANGED" });
    })()
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();

  const target = safeTaskUrl(event.notification.data?.url || "/");

  event.waitUntil(
    (async () => {
      await postToClients({
        type: "NOTIFICATION_CLICKED",
        task_id: event.notification.data?.task_id || null,
        notification_id: event.notification.data?.notification_id || null,
        voice_text: event.notification.data?.voice_text || null,
        notification_type: event.notification.data?.notification_type || null,
        priority: event.notification.data?.priority || null,
      });

      try {
        const clientList = await self.clients.matchAll({
          type: "window",
          includeUncontrolled: true,
        });

        for (const client of clientList) {
          if (client.url && client.url.startsWith(self.location.origin)) {
            if ("navigate" in client) {
              try {
                await client.navigate(target);
              } catch {
                // fall through to focus
              }
            }

            try {
              return await client.focus();
            } catch {
              // fall through to openWindow
            }
          }
        }
      } catch {
        // fall through to openWindow
      }

      try {
        return await self.clients.openWindow(target);
      } catch {
        return undefined;
      }
    })()
  );
});
