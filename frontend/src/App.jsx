import { useCallback, useEffect, useRef, useState } from "react";
import "./App.css";
import {
  API_BASE as API,
  authErrorMessage,
  beginGoogleLogin,
  bootstrapCsrfToken,
  clearCsrfToken,
  logoutRequest,
  refreshSessionRequest,
} from "./auth";
import {
  assignableRoles,
  canCreateTasks,
  canDeleteTasks,
  canManageEmployee,
  canManageEmployees,
  canReadAllTasks as hasReadAllTasks,
  canUpdateProgress,
} from "./permissions";
import {
  NOTIFICATION_POLL_MS,
  applyAllNotificationsRead,
  applyNotificationRead,
  base64UrlToUint8Array,
  buildPushSubscriptionPayload,
  normalizeNotificationList,
  notificationDeepLink,
  notificationIdFromSearch,
  pushPermissionState,
  unreadBadgeLabel,
} from "./notifications";
import { welcomeMessage } from "./greeting";
import {
  createAlertController,
  diffNewNotifications,
  loadAlertPreferences,
  notificationIds,
  saveAlertPreferences,
} from "./assistantAlerts";

// The backend runs on port 8000 (see the root README). A different port
// (for example a test server) can be used with VITE_API_BASE_URL, e.g.
// VITE_API_BASE_URL=http://localhost:8001 npm run dev
// The default in ./auth.js is "http://localhost:8000".
const CHAT_API = `${API}/tasks/chat`;
const VOICE_TURN_API = `${API}/tasks/voice/turn`;
const FEEDBACK_API = `${API}/tasks/feedback`;
const TASKS_API = `${API}/tasks`;
const EMPLOYEES_API = `${API}/employees`;
const NOTIFICATIONS_API = `${API}/notifications`;
const NOTIFICATION_SETTINGS_API = `${NOTIFICATIONS_API}/settings`;
const PUSH_API = `${API}/push`;
const EMPLOYEE_ROLES = ["ADMIN", "MANAGER", "EMPLOYEE"];

const FIELD_META = {
  task_title: { label: "Task title" },
  task_description: { label: "Description" },
  assigned_to: { label: "Assigned to" },
  assigned_date: { label: "Assigned date" },
  assigned_time: { label: "Assigned time" },
  deadline_date: { label: "Deadline date" },
  deadline_time: { label: "Deadline time" },
  reminder_date: { label: "Reminder date" },
  reminder_time: { label: "Reminder time" },
  status: { label: "Status" },
  progress: { label: "Progress" },
};

const FIELD_ORDER = Object.keys(FIELD_META);
const DEFAULT_REQUIRED_FIELDS = [
  "task_title",
  "task_description",
  "assigned_to",
  "deadline_date",
  "deadline_time",
];

const formatDate = (value) => {
  if (!value) return "";

  const parsed = new Date(`${value}T00:00:00`);

  if (Number.isNaN(parsed.getTime())) {
    return value;
  }

  return parsed.toLocaleDateString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
};

const formatTime = (value) => (value ? value.slice(0, 5) : "");

const formatDateTime = (value) => {
  if (!value) return "—";

  const parsed = new Date(value);

  if (Number.isNaN(parsed.getTime())) {
    return value;
  }

  return parsed.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
};

const statusClass = (value) =>
  (value || "pending")
    .toLowerCase()
    .replaceAll("_", "-")
    .replace(/\s+/g, "-");

// The id of the conversation. The backend keeps a small memory of it, so a
// follow-up message ("change its deadline") can be understood. A new
// conversation starts with a new id, and never reuses the memory of an
// older one.
const newConversationId = () => {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }

  return `conversation-${Date.now()}-${Math.random().toString(16).slice(2)}`;
};

const roleLabel = (role) => {
  if (!role) return "";
  return role.charAt(0) + role.slice(1).toLowerCase();
};

// Lucide icons, inlined so the feedback buttons need no extra package.
function ThumbsUpIcon() {
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M7 10v12" />
      <path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z" />
    </svg>
  );
}

function BellIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
      <path d="M13.73 21a2 2 0 0 1-3.46 0" />
    </svg>
  );
}

function ThumbsDownIcon() {
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M17 14V2" />
      <path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z" />
    </svg>
  );
}

const isTaskOverdue = (task) => {
  if (!task?.deadline_date || task.status === "COMPLETED") {
    return false;
  }

  const deadline = new Date(
    `${task.deadline_date}T${task.deadline_time || "23:59"}:00`
  );

  return !Number.isNaN(deadline.getTime()) && deadline < new Date();
};

function DateCell({ date, time }) {
  if (!date && !time) {
    return <span className="muted-cell">Not set</span>;
  }

  return (
    <div className="cell-stack">
      <span>{formatDate(date) || "No date"}</span>
      {time && <span className="cell-muted">{formatTime(time)}</span>}
    </div>
  );
}

const authErrorFromUrl = () => {
  if (typeof window === "undefined") return "";

  const errorCode = new URLSearchParams(window.location.search).get(
    "auth_error"
  );

  return errorCode ? authErrorMessage(errorCode) : "";
};

function App() {
  const [auth, setAuth] = useState({
    status: "loading",
    accessToken: null,
    employee: null,
    expiresIn: 0,
  });
  const [authError, setAuthError] = useState(authErrorFromUrl);
  const [authBusy, setAuthBusy] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const accessTokenRef = useRef(null);
  const refreshPromiseRef = useRef(null);
  const apiFetchRef = useRef(null);
  const employeeIdentityRef = useRef(null);

  const [messages, setMessages] = useState([
    { id: "welcome", role: "assistant", text: welcomeMessage(null) },
  ]);
  const [input, setInput] = useState("");

  const [stage, setStage] = useState(null);
  const [draft, setDraft] = useState(null);
  const [status, setStatus] = useState(null);
  const [missingFields, setMissingFields] = useState([]);
  const [requiredFields, setRequiredFields] = useState(DEFAULT_REQUIRED_FIELDS);
  const [savedTask, setSavedTask] = useState(null);
  const [savedMessage, setSavedMessage] = useState("");

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const [conversationId, setConversationId] = useState(newConversationId);

  const [activeTab, setActiveTab] = useState("assistant");
  const [tasks, setTasks] = useState([]);
  const [tasksLoading, setTasksLoading] = useState(false);
  const [tasksError, setTasksError] = useState("");
  const [tasksFetchedAt, setTasksFetchedAt] = useState("");
  const [taskMessage, setTaskMessage] = useState("");
  const [progressDrafts, setProgressDrafts] = useState({});
  const [taskActionId, setTaskActionId] = useState(null);
  const [highlightedTaskId, setHighlightedTaskId] = useState(null);
  const [taskDialog, setTaskDialog] = useState(null);
  const [taskForm, setTaskForm] = useState(null);
  const [taskSaving, setTaskSaving] = useState(false);
  const [taskFormError, setTaskFormError] = useState("");


  const [notifications, setNotifications] = useState([]);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [notificationsLoading, setNotificationsLoading] = useState(false);
  const [notificationsError, setNotificationsError] = useState("");
  const [notificationActionId, setNotificationActionId] = useState(null);
  const [unreadCount, setUnreadCount] = useState(0);
  const [notificationTimezone, setNotificationTimezone] = useState("Asia/Kolkata");
  const [timezoneDraft, setTimezoneDraft] = useState("Asia/Kolkata");
  const [timezoneSaving, setTimezoneSaving] = useState(false);
  const [pushState, setPushState] = useState("prompt");
  const [pushBusy, setPushBusy] = useState(false);
  const [pushMessage, setPushMessage] = useState("");
  const [pushConfigured, setPushConfigured] = useState(false);
  const [alertPrefs, setAlertPrefs] = useState(loadAlertPreferences);
  const [alertStatus, setAlertStatus] = useState("");
  const serviceWorkerRef = useRef(null);
  const alertControllerRef = useRef(null);
  const notificationMenuRef = useRef(null);
  const seenNotificationIdsRef = useRef(new Set());
  const notificationsLoadedRef = useRef(false);
  const deepLinkOpenedRef = useRef(false);
  const testAlertSeqRef = useRef(0);
  const activeNotificationIdRef = useRef(null);

  const [employees, setEmployees] = useState([]);
  const [employeeSummary, setEmployeeSummary] = useState({
    total: 0,
    active: 0,
    inactive: 0,
    not_logged_in: 0,
  });
  const [employeeRoles, setEmployeeRoles] = useState(EMPLOYEE_ROLES);
  const [employeesLoading, setEmployeesLoading] = useState(false);
  const [employeesError, setEmployeesError] = useState("");
  const [employeeMessage, setEmployeeMessage] = useState("");
  const [employeeSearch, setEmployeeSearch] = useState("");
  const [employeeRoleFilter, setEmployeeRoleFilter] = useState("");
  const [employeeStatusFilter, setEmployeeStatusFilter] = useState("");
  const [employeeDialog, setEmployeeDialog] = useState(null);
  const [employeeForm, setEmployeeForm] = useState({
    name: "",
    email: "",
    role: "EMPLOYEE",
  });
  const [employeeSaving, setEmployeeSaving] = useState(false);
  const [employeeConfirm, setEmployeeConfirm] = useState(null);

  const [recorder, setRecorder] = useState(null);
  const [recording, setRecording] = useState(false);
  const [audio, setAudio] = useState(null);
  const [audioUrl, setAudioUrl] = useState("");

  // The authenticated identity is external state; reset the conversation
  // when it changes instead of leaking one employee into another session.
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    const employeeId = auth.employee?.id ?? null;
    const employeeName = auth.employee?.name ?? "";

    if (employeeId === null) {
      employeeIdentityRef.current = null;
      setMessages([
        { id: "welcome", role: "assistant", text: welcomeMessage(null) },
      ]);
      setConversationId(newConversationId());
      setStage(null);
      setDraft(null);
      setStatus(null);
      setMissingFields([]);
      setSavedTask(null);
      setError("");
      return;
    }

    const changed =
      employeeIdentityRef.current !== null &&
      String(employeeIdentityRef.current) !== String(employeeId);

    employeeIdentityRef.current = employeeId;

    if (changed) {
      setMessages([
        {
          id: `welcome-${Date.now()}`,
          role: "assistant",
          text: welcomeMessage({ name: employeeName }),
        },
      ]);
      setInput("");
      setStage(null);
      setDraft(null);
      setStatus(null);
      setMissingFields([]);
      setRequiredFields(DEFAULT_REQUIRED_FIELDS);
      setSavedTask(null);
      setSavedMessage("");
      setError("");
      setAudio(null);
      setAudioUrl("");
      setConversationId(newConversationId());
      setTasks([]);
      return;
    }

    setMessages((current) => {
      if (current.length === 0) {
        return [
          {
            id: "welcome",
            role: "assistant",
            text: welcomeMessage({ name: employeeName }),
          },
        ];
      }

      const [first, ...rest] = current;

      if (
        first?.role !== "assistant" ||
        !String(first.id).startsWith("welcome")
      ) {
        return current;
      }

      return [
        {
          ...first,
          text: welcomeMessage({ name: employeeName }),
        },
        ...rest,
      ];
    });
  }, [auth.employee?.id, auth.employee?.name]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const speak = (message) => {
    if (
      typeof window === "undefined" ||
      !window.speechSynthesis ||
      !message
    ) {
      return;
    }

    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(message);
    utterance.lang = "en-IN";
    window.speechSynthesis.speak(utterance);
  };

  const refreshSession = useCallback(async () => {
    if (refreshPromiseRef.current) {
      return refreshPromiseRef.current;
    }

    const promise = (async () => {
      const applySignedOut = (message = "") => {
        accessTokenRef.current = null;
        clearCsrfToken();
        setAuth({
          status: "signed_out",
          accessToken: null,
          employee: null,
          expiresIn: 0,
        });

        if (message) {
          setAuthError(message);
        }

        return null;
      };

      const applyAuthenticated = (data) => {
        accessTokenRef.current = data.access_token;
        setAuth({
          status: "authenticated",
          accessToken: data.access_token,
          employee: data.employee,
          expiresIn: Number(data.expires_in) || 0,
        });
        setAuthError("");

        return data.access_token;
      };

      try {
        try {
          await bootstrapCsrfToken();
        } catch {
          return applySignedOut("Unable to reach the authentication service.");
        }

        for (let attempt = 0; attempt < 2; attempt += 1) {
          const result = await refreshSessionRequest();

          if (result?.ok && result.data?.access_token && result.data?.employee) {
            return applyAuthenticated(result.data);
          }

          const detail = result?.data?.detail || "";

          if (
            attempt === 0 &&
            result?.status === 401 &&
            /already been rotated/i.test(detail)
          ) {
            continue;
          }

          if (result?.status === 403) {
            return applySignedOut();
          }

          if (/unable to reach|network|initialize the session/i.test(detail)) {
            return applySignedOut("Unable to reach the authentication service.");
          }

          return applySignedOut();
        }

        return applySignedOut();
      } catch {
        return applySignedOut("Unable to reach the authentication service.");
      }
    })();

    refreshPromiseRef.current = promise;

    try {
      return await promise;
    } finally {
      refreshPromiseRef.current = null;
    }
  }, []);

  const apiFetch = useCallback(
    async (url, options = {}, retry = true) => {
      const headers = new Headers(options.headers || {});
      const token = accessTokenRef.current;

      if (token) {
        headers.set("Authorization", `Bearer ${token}`);
      }

      const response = await fetch(url, { ...options, headers });

      if (response.status === 401 && retry && token) {
        const refreshed = await refreshSession();

        if (refreshed) {
          return apiFetchRef.current(url, options, false);
        }
      }

      return response;
    },
    [refreshSession]
  );

  useEffect(() => {
    apiFetchRef.current = apiFetch;
  }, [apiFetch]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const errorCode = params.get("auth_error");

    if (errorCode) {
      params.delete("auth_error");
      const query = params.toString();
      window.history.replaceState(
        {},
        "",
        `${window.location.pathname}${query ? `?${query}` : ""}`
      );
    }

    refreshSession();
  }, [refreshSession]);

  useEffect(() => {
    if (auth.status !== "authenticated" || !auth.expiresIn) return undefined;

    const refreshIn = Math.max(30_000, auth.expiresIn * 1000 - 60_000);
    const timer = window.setTimeout(refreshSession, refreshIn);

    return () => window.clearTimeout(timer);
  }, [auth.status, auth.expiresIn, refreshSession]);

  const startLogin = async () => {
    setAuthBusy(true);
    setAuthError("");

    try {
      await beginGoogleLogin(window.location.href);
    } catch (err) {
      setAuthError(err.message);
      setAuthBusy(false);
    }
  };

  const signOut = async (allSessions = false) => {
    setAuthBusy(true);

    try {
      await logoutRequest(accessTokenRef.current, allSessions);
    } finally {
      accessTokenRef.current = null;
      setAuth({
        status: "signed_out",
        accessToken: null,
        employee: null,
        expiresIn: 0,
      });
      setProfileOpen(false);
      setTasks([]);
      setEmployees([]);
      setNotifications([]);
      setNotificationsOpen(false);
      setUnreadCount(0);
      seenNotificationIdsRef.current = new Set();
      notificationsLoadedRef.current = false;
      alertControllerRef.current?.stopSpeech();
      setAuthBusy(false);
    }
  };

  const loadTasks = useCallback(async () => {
    setTasksLoading(true);
    setTasksError("");

    try {
      const response = await apiFetch(TASKS_API);
      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || data.message || "Unable to load tasks.");
      }

      setTasks(Array.isArray(data.tasks) ? data.tasks : []);
      setTasksFetchedAt(
        new Date().toLocaleTimeString("en-IN", {
          hour: "2-digit",
          minute: "2-digit",
        })
      );
    } catch (err) {
      setTasksError(err.message);
    } finally {
      setTasksLoading(false);
    }
  }, [apiFetch]);

  const loadUnreadCount = useCallback(async () => {
    try {
      const response = await apiFetch(`${NOTIFICATIONS_API}/unread-count`);
      const data = await response.json().catch(() => ({}));

      if (response.ok) {
        setUnreadCount(Number(data.unread_count) || 0);
      }
    } catch {
      // A background count failure must not interrupt the workspace.
    }
  }, [apiFetch]);

  const loadNotifications = useCallback(async ({ announce = true } = {}) => {
    setNotificationsLoading(true);
    setNotificationsError("");

    try {
      const response = await apiFetch(`${NOTIFICATIONS_API}?limit=50`);
      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        throw new Error(
          data.detail || "Unable to load notifications."
        );
      }

      const list = normalizeNotificationList(data);
      setNotifications(list);
      setUnreadCount(Number(data.unread_count) || 0);

      const seen = seenNotificationIdsRef.current;
      if (!notificationsLoadedRef.current) {
        seenNotificationIdsRef.current = notificationIds(list);
        notificationsLoadedRef.current = true;
      } else {
        const fresh = diffNewNotifications(list, seen);
        seenNotificationIdsRef.current = new Set([
          ...seen,
          ...notificationIds(list),
        ]);

        if (announce) {
          const visible =
            typeof document === "undefined" ||
            document.visibilityState === "visible";

          for (const item of fresh) {
            if (!item.is_read) {
              alertControllerRef.current?.handleNotification(item, {
                allowSpeech: visible,
              });
            }
          }
        }
      }
    } catch (err) {
      setNotificationsError(err.message);
    } finally {
      setNotificationsLoading(false);
    }
  }, [apiFetch]);

  const loadNotificationSettings = useCallback(async () => {
    try {
      const response = await apiFetch(NOTIFICATION_SETTINGS_API);
      const data = await response.json().catch(() => ({}));

      if (!response.ok) return;

      const timezone = data.timezone || "Asia/Kolkata";
      setNotificationTimezone(timezone);
      setTimezoneDraft(timezone);
      setPushConfigured(Boolean(data.push_configured));
    } catch {
      // Settings are optional; delivery and the in-app center still work.
    }
  }, [apiFetch]);

  const registerServiceWorker = useCallback(async () => {
    if (
      typeof navigator === "undefined" ||
      !("serviceWorker" in navigator)
    ) {
      setPushState("unsupported");
      return null;
    }

    try {
      const registration = await navigator.serviceWorker.register("/sw.js", {
        scope: "/",
        updateViaCache: "none",
      });
      serviceWorkerRef.current = registration;

      // Pick up a fixed service worker without requiring a hard refresh.
      registration.update().catch(() => {});
      navigator.serviceWorker.ready
        .then((ready) => {
          serviceWorkerRef.current = ready;
          ready.update?.().catch(() => {});
        })
        .catch(() => {});

      return registration;
    } catch {
      setPushState("unsupported");
      return null;
    }
  }, []);

  const refreshPushState = useCallback(async () => {
    const supported =
      typeof window !== "undefined" &&
      "serviceWorker" in navigator &&
      "PushManager" in window &&
      typeof Notification !== "undefined";

    if (!supported) {
      setPushState("unsupported");
      return;
    }

    try {
      const registration =
        serviceWorkerRef.current ||
        (await navigator.serviceWorker.getRegistration());
      const subscription = await registration?.pushManager.getSubscription();

      setPushState(
        pushPermissionState({
          supported,
          permission: Notification.permission,
          subscribed: Boolean(subscription),
        })
      );
    } catch {
      setPushState(
        pushPermissionState({
          supported,
          permission: Notification.permission,
          subscribed: false,
        })
      );
    }
  }, []);

  useEffect(() => {
    alertControllerRef.current = createAlertController({
      getPreferences: () => alertPrefs,
      onSoundError: (message) => setAlertStatus(String(message || "Audio unavailable.")),
    });

    return () => {
      alertControllerRef.current?.dispose();
      alertControllerRef.current = null;
    };
  }, [alertPrefs]);

  useEffect(() => {
    saveAlertPreferences(alertPrefs);
  }, [alertPrefs]);

  const updateAlertPrefs = (patch) => {
    setAlertPrefs((current) => ({ ...current, ...patch }));
  };

  const enableAttentionAlerts = async () => {
    updateAlertPrefs({ attentionEnabled: true });
    const ready = await alertControllerRef.current?.ensureAudioReady();
    setAlertStatus(
      ready
        ? "Voice and alarm alerts are active for this open session."
        : "Attention enabled. Browser audio may require another interaction."
    );
  };

  const testAttentionAlerts = async () => {
    const sample = {
      id: `test-alert-${++testAlertSeqRef.current}`,
      priority: "HIGH",
      notification_type: "TASK_REMINDER",
      title: "Task reminder",
      message: `Hey ${employee?.name?.split(" ")[0] || "there"}, reminder for your task.`,
      voice_text: `Hey ${employee?.name?.split(" ")[0] || "there"}, this is a reminder for you. Your task is the sample task. You have 10 minutes remaining. Please complete it on time.`,
    };
    await alertControllerRef.current?.ensureAudioReady();
    if (alertPrefs.soundEnabled) {
      await alertControllerRef.current?.playTone("HIGH");
    }
    if (alertPrefs.voiceEnabled) {
      alertControllerRef.current?.handleNotification(sample, { allowSpeech: true });
    }
    setAlertStatus("Played a sample spoken reminder and alarm (if enabled).");
  };

  const openTaskFromNotification = useCallback((taskId) => {
    setNotificationsOpen(false);
    setActiveTab("tasks");
    setHighlightedTaskId(taskId);
    setTaskMessage(`Opened task #${taskId} from a reminder.`);
    loadTasks();
  }, [loadTasks]);

  const markNotificationRead = useCallback(async (notificationId) => {
    setNotificationActionId(notificationId);

    try {
      const response = await apiFetch(
        `${NOTIFICATIONS_API}/${notificationId}/read`,
        { method: "PATCH" }
      );
      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        throw new Error(data.detail || "Unable to mark the notification read.");
      }

      setNotifications((current) =>
        applyNotificationRead(current, notificationId)
      );
      setUnreadCount(Number(data.unread_count) || 0);
      return true;
    } catch (err) {
      setNotificationsError(err.message);
      return false;
    } finally {
      setNotificationActionId(null);
    }
  }, [apiFetch]);

  const respondToNotification = async (notification, action) => {
    setNotificationActionId(notification.id);
    try {
      const response = await apiFetch(`${NOTIFICATIONS_API}/${notification.id}/followup/respond`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "Unable to update notification.");
      setNotifications((current) =>
        current.map((item) =>
          item.id === notification.id
            ? { ...item, is_read: true, payload: { ...(item.payload || {}), followup_status: data.followup_status } }
            : item
        )
      );
      setUnreadCount((c) => Math.max(0, c - (notification.is_read ? 0 : 1)));
      if (action === "accept") {
        activeNotificationIdRef.current = notification.id;
        setNotificationsOpen(false);
        setActiveTab("assistant");
        // Clear stale task-creation state; notification follow-up is an
        // existing-task workflow and must never inherit a CREATE draft.
        setStage(null);
        setDraft(null);
        setMissingFields([]);
        setRequiredFields(DEFAULT_REQUIRED_FIELDS);
        setSavedTask(null);
        setSavedMessage("");
        setStatus(null);
        // Establish task context through existing chat flow.
        submitMessage("Yes, I want to complete the missing task details.", {
          notification_id: notification.id,
          stage: null,
          draft: null,
        });
      }
      return data;
    } catch (err) {
      setNotificationsError(err.message);
      return null;
    } finally {
      setNotificationActionId(null);
    }
  };

  const openNotification = async (notification) => {
    if (!notification.is_read) {
      await markNotificationRead(notification.id);
    }
    const payload = notification.payload || {};
    const isFollowup =
      notification.notification_type === "TASK_MISSING_DETAILS" ||
      payload.kind === "task_missing_details_followup" ||
      payload.kind === "task_assignment";
    const hasActions = Array.isArray(payload.actions) && payload.actions.length > 0;
    if (isFollowup && hasActions) {
      // Actionable notification: don't auto-open; buttons handle it.
      return;
    }
    if (isFollowup) {
      activeNotificationIdRef.current = notification.id;
      setActiveTab("assistant");
      setNotificationsOpen(false);
      submitMessage("I received a task follow-up notification. What details are missing?");
      return;
    }
    openTaskFromNotification(notification.task_id);
  };

  const markAllNotificationsRead = async () => {
    setNotificationActionId("all");

    try {
      const response = await apiFetch(`${NOTIFICATIONS_API}/read-all`, {
        method: "POST",
      });
      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        throw new Error(data.detail || "Unable to mark notifications read.");
      }

      setNotifications((current) => applyAllNotificationsRead(current));
      setUnreadCount(0);
    } catch (err) {
      setNotificationsError(err.message);
    } finally {
      setNotificationActionId(null);
    }
  };

  const deleteNotification = async (notificationId) => {
    setNotificationActionId(`delete-${notificationId}`);
    try {
      const response = await apiFetch(`${NOTIFICATIONS_API}/${notificationId}`, {
        method: "DELETE",
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || "Unable to delete notification.");
      }
      setNotifications((current) => current.filter((item) => item.id !== notificationId));
      setUnreadCount(Number(data.unread_count) || 0);
    } catch (err) {
      setNotificationsError(err.message);
    } finally {
      setNotificationActionId(null);
    }
  };

  const deleteAllNotifications = async () => {
    if (notifications.length === 0) return;
    if (!window.confirm("Delete all notifications?")) return;
    setNotificationActionId("delete-all");
    try {
      const response = await apiFetch(NOTIFICATIONS_API, { method: "DELETE" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || "Unable to delete notifications.");
      }
      setNotifications([]);
      setUnreadCount(0);
    } catch (err) {
      setNotificationsError(err.message);
    } finally {
      setNotificationActionId(null);
    }
  };

  const toggleNotifications = () => {
    setNotificationsOpen((open) => {
      const next = !open;
      if (next) loadNotifications();
      return next;
    });
  };

  useEffect(() => {
    if (!notificationsOpen) return undefined;
    const handlePointerDown = (event) => {
      if (notificationMenuRef.current && !notificationMenuRef.current.contains(event.target)) {
        setNotificationsOpen(false);
      }
    };
    const handleKeyDown = (event) => {
      if (event.key === "Escape") setNotificationsOpen(false);
    };
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("touchstart", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("touchstart", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [notificationsOpen]);

  const saveNotificationTimezone = async (event) => {
    event.preventDefault();
    setTimezoneSaving(true);
    setNotificationsError("");

    try {
      const response = await apiFetch(NOTIFICATION_SETTINGS_API, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ timezone: timezoneDraft.trim() }),
      });
      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        throw new Error(data.detail || "Unable to save the timezone.");
      }

      setNotificationTimezone(data.timezone);
      setTimezoneDraft(data.timezone);
      setAuth((current) => ({
        ...current,
        employee: {
          ...current.employee,
          timezone: data.timezone,
        },
      }));
      setPushMessage(`Reminders use ${data.timezone}.`);
    } catch (err) {
      setNotificationsError(err.message);
    } finally {
      setTimezoneSaving(false);
    }
  };

  const enablePushNotifications = async () => {
    setPushBusy(true);
    setPushMessage("");
    setNotificationsError("");

    try {
      if (
        typeof Notification === "undefined" ||
        !("serviceWorker" in navigator) ||
        !("PushManager" in window)
      ) {
        setPushState("unsupported");
        setPushMessage("This browser does not support Web Push.");
        return;
      }

      const permission = await Notification.requestPermission();

      if (permission !== "granted") {
        setPushState(permission === "denied" ? "denied" : "prompt");
        setPushMessage(
          permission === "denied"
            ? "Notifications are blocked in the browser settings."
            : "Notification permission was not granted."
        );
        return;
      }

      const keyResponse = await apiFetch(`${PUSH_API}/public-key`);
      const keyData = await keyResponse.json().catch(() => ({}));

      if (!keyResponse.ok || !keyData.configured || !keyData.public_key) {
        setPushConfigured(false);
        setPushMessage(
          "Web Push is not configured on the server. In-app notifications still work."
        );
        return;
      }

      setPushConfigured(true);
      const registration =
        serviceWorkerRef.current ||
        (await registerServiceWorker());

      if (!registration) {
        setPushMessage("The notification service worker is unavailable.");
        return;
      }

      let subscription = await registration.pushManager.getSubscription();

      if (!subscription) {
        subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: base64UrlToUint8Array(keyData.public_key),
        });
      }

      const deviceName = String(
        navigator.userAgentData?.platform ||
          navigator.platform ||
          navigator.userAgent ||
          "Browser"
      ).slice(0, 120);

      const response = await apiFetch(`${PUSH_API}/subscribe`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          buildPushSubscriptionPayload(subscription, deviceName)
        ),
      });
      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        throw new Error(data.detail || "Unable to register this device.");
      }

      setPushState("subscribed");
      setPushMessage("This device will receive task reminders.");
    } catch (err) {
      setNotificationsError(err.message);
      setPushMessage("Push registration failed.");
    } finally {
      setPushBusy(false);
    }
  };

  const disablePushNotifications = async () => {
    setPushBusy(true);
    setPushMessage("");
    setNotificationsError("");

    try {
      const registration =
        serviceWorkerRef.current ||
        (await navigator.serviceWorker.getRegistration());
      const subscription = await registration?.pushManager.getSubscription();

      if (subscription) {
        try {
          await apiFetch(`${PUSH_API}/subscribe`, {
            method: "DELETE",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ endpoint: subscription.endpoint }),
          });
        } catch {
          // Local unsubscribe should still proceed.
        }
        await subscription.unsubscribe();
      }

      await refreshPushState();
      setPushMessage("Push notifications are disabled on this device.");
    } catch (err) {
      setNotificationsError(err.message);
    } finally {
      setPushBusy(false);
    }
  };

  useEffect(() => {
    if (auth.status !== "authenticated") {
      return undefined;
    }

    let cancelled = false;

    const bootstrap = async () => {
      const registration = await registerServiceWorker();
      if (cancelled) return;

      if (registration) {
        await refreshPushState();
      }

      await loadNotifications({ announce: false });
      await loadUnreadCount();
      await loadNotificationSettings();
    };

    bootstrap();
    const timer = window.setInterval(() => {
      loadNotifications();
      loadUnreadCount();
    }, NOTIFICATION_POLL_MS);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [
    auth.status,
    loadNotificationSettings,
    loadNotifications,
    loadUnreadCount,
    refreshPushState,
    registerServiceWorker,
  ]);

  useEffect(() => {
    if (
      auth.status !== "authenticated" ||
      deepLinkOpenedRef.current ||
      typeof window === "undefined"
    ) {
      return;
    }

    const taskId = notificationDeepLink(window.location.search);

    if (!taskId) return;

    deepLinkOpenedRef.current = true;
    const notificationId = notificationIdFromSearch(window.location.search);
    const isFollowup = new URLSearchParams(window.location.search).get('followup') === '1';
    const timer = window.setTimeout(() => {
      if (notificationId) {
        markNotificationRead(notificationId);
      }
      if (notificationId && isFollowup) {
        activeNotificationIdRef.current = notificationId;
        setActiveTab('assistant');
        submitMessage('I opened a task follow-up notification. What details are missing?');
        return;
      }
      openTaskFromNotification(taskId);
    }, 0);

    const url = new URL(window.location.href);
    url.searchParams.delete("task");
    url.searchParams.delete("notification");
    url.searchParams.delete("followup");
    window.history.replaceState(
      {},
      "",
      `${url.pathname}${url.search}${url.hash}`
    );

    return () => window.clearTimeout(timer);
  }, [
    auth.status,
    markNotificationRead,
    openTaskFromNotification,
  ]);

  useEffect(() => {
    if (
      typeof navigator === "undefined" ||
      !("serviceWorker" in navigator)
    ) {
      return undefined;
    }

    const handleMessage = (event) => {
      const type = event.data?.type;

      if (type === "PUSH_RECEIVED") {
        // A push was received by the service worker. When the app is open this
        // loads the notification and (if enabled) plays alarm + speaks it.
        loadNotifications();
        loadUnreadCount();
        return;
      }

      if (type === "NOTIFICATION_CLICKED") {
        const taskId = Number(event.data?.task_id);
        const notificationId = Number(event.data?.notification_id);

        if (Number.isInteger(notificationId) && notificationId > 0) {
          markNotificationRead(notificationId);
        }

        if (Number.isInteger(taskId) && taskId > 0) {
          openTaskFromNotification(taskId);
        }

        // Clicking the OS notification is a user gesture; speak the reminder
        // now if the app is open and voice alerts are enabled.
        if (event.data?.voice_text && alertControllerRef.current) {
          alertControllerRef.current.handleNotification(
            {
              id:
                Number.isInteger(notificationId) && notificationId > 0
                  ? notificationId
                  : `sw-click-${taskId || "task"}`,
              notification_type: event.data.notification_type || "TASK_REMINDER",
              priority: event.data.priority || "HIGH",
              voice_text: event.data.voice_text,
              is_read: false,
            },
            { allowSpeech: true }
          );
        }
      }
    };

    navigator.serviceWorker.addEventListener("message", handleMessage);

    return () =>
      navigator.serviceWorker.removeEventListener("message", handleMessage);
  }, [
    loadNotifications,
    loadUnreadCount,
    markNotificationRead,
    openTaskFromNotification,
  ]);

  useEffect(() => {
    if (
      activeTab !== "tasks" ||
      !highlightedTaskId ||
      tasksLoading ||
      typeof document === "undefined"
    ) {
      return undefined;
    }

    const element = document.getElementById(`task-row-${highlightedTaskId}`);

    if (element) {
      element.scrollIntoView({ behavior: "smooth", block: "center" });
      const timer = window.setTimeout(
        () => setHighlightedTaskId(null),
        4500
      );

      return () => window.clearTimeout(timer);
    }

    return undefined;
  }, [activeTab, highlightedTaskId, tasksLoading]);

  const saveTaskProgress = async (task) => {
    const raw = progressDrafts[task.id] ?? task.progress ?? 0;
    const progress = Number(raw);

    setTaskMessage("");
    setTasksError("");

    if (!Number.isInteger(progress) || progress < 0 || progress > 100) {
      setTasksError("Progress must be a whole number from 0 to 100.");
      return;
    }

    setTaskActionId(task.id);

    try {
      const response = await apiFetch(`${TASKS_API}/${task.id}/progress`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ progress }),
      });
      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        throw new Error(data.detail || "Unable to update progress.");
      }

      setTasks((current) =>
        current.map((row) => (row.id === task.id ? data.task : row))
      );
      setProgressDrafts((current) => {
        const next = { ...current };
        delete next[task.id];
        return next;
      });
      setTaskMessage(`Task #${task.id} progress saved.`);
    } catch (err) {
      setTasksError(err.message);
    } finally {
      setTaskActionId(null);
    }
  };

  const openEditTask = (task) => {
    setTaskDialog(task);
    setTaskForm({
      task_title: task.task_title || "",
      task_description: task.task_description || "",
      assigned_date: task.assigned_date || "",
      assigned_time: formatTime(task.assigned_time),
      deadline_date: task.deadline_date || "",
      deadline_time: formatTime(task.deadline_time),
      reminder_date: task.reminder_date || "",
      reminder_time: formatTime(task.reminder_time),
      status: task.status || "PENDING",
      progress: String(task.progress ?? 0),
    });
    setTaskFormError("");
  };

  const closeTaskDialog = () => {
    if (taskSaving) return;
    setTaskDialog(null);
    setTaskForm(null);
    setTaskFormError("");
  };

  const submitTaskForm = async (event) => {
    event.preventDefault();

    if (!taskDialog || !taskForm) return;

    const progress = Number(taskForm.progress);
    const title = taskForm.task_title.trim();

    setTaskFormError("");

    if (!title) {
      setTaskFormError("A task title is required.");
      return;
    }

    if (!Number.isInteger(progress) || progress < 0 || progress > 100) {
      setTaskFormError("Progress must be a whole number from 0 to 100.");
      return;
    }

    const optional = (value) => {
      const text = String(value || "").trim();
      return text === "" ? null : text;
    };

    setTaskSaving(true);

    try {
      const response = await apiFetch(`${TASKS_API}/${taskDialog.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_title: title,
          task_description: optional(taskForm.task_description),
          assigned_date: optional(taskForm.assigned_date),
          assigned_time: optional(taskForm.assigned_time),
          deadline_date: optional(taskForm.deadline_date),
          deadline_time: optional(taskForm.deadline_time),
          reminder_date: optional(taskForm.reminder_date),
          reminder_time: optional(taskForm.reminder_time),
          status: taskForm.status,
          progress,
        }),
      });
      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        throw new Error(data.detail || "Unable to update the task.");
      }

      setTasks((current) =>
        current.map((row) => (row.id === taskDialog.id ? data.task : row))
      );
      setTaskMessage(`Task #${taskDialog.id} updated.`);
      setTaskDialog(null);
      setTaskForm(null);
    } catch (err) {
      setTaskFormError(err.message);
    } finally {
      setTaskSaving(false);
    }
  };

  const requestDeleteTask = async (task) => {
    const phrase = `CONFIRM DELETE`;

    if (!window.confirm(`Delete task #${task.id} "${task.task_title}"?`)) {
      return;
    }

    const confirmation = window.prompt(
      `Type ${phrase} to permanently delete this task.`
    );

    if (confirmation !== phrase) {
      setTasksError(`The task was not deleted. Type ${phrase} exactly.`);
      return;
    }

    setTaskMessage("");
    setTasksError("");
    setTaskActionId(task.id);

    try {
      const response = await apiFetch(`${TASKS_API}/${task.id}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirmation }),
      });
      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        throw new Error(data.detail || "Unable to delete the task.");
      }

      setTasks((current) => current.filter((row) => row.id !== task.id));
      setTaskMessage(`Task #${task.id} was deleted.`);
    } catch (err) {
      setTasksError(err.message);
    } finally {
      setTaskActionId(null);
    }
  };

  const loadEmployees = useCallback(async () => {
    setEmployeesLoading(true);
    setEmployeesError("");

    try {
      const params = new URLSearchParams();

      if (employeeSearch.trim()) {
        params.set("search", employeeSearch.trim());
      }

      if (employeeRoleFilter) {
        params.set("role", employeeRoleFilter);
      }

      if (employeeStatusFilter) {
        params.set("status", employeeStatusFilter);
      }

      const query = params.toString();
      const response = await apiFetch(
        `${EMPLOYEES_API}${query ? `?${query}` : ""}`
      );
      const data = await response.json();

      if (!response.ok) {
        throw new Error(
          data.detail || data.message || "Unable to load employees."
        );
      }

      setEmployees(Array.isArray(data.employees) ? data.employees : []);
      setEmployeeSummary({
        total: Number(data.total) || 0,
        active: Number(data.active) || 0,
        inactive: Number(data.inactive) || 0,
        not_logged_in: Number(data.not_logged_in) || 0,
      });

      if (Array.isArray(data.roles) && data.roles.length > 0) {
        setEmployeeRoles(data.roles);
      }
    } catch (err) {
      setEmployeesError(err.message);
    } finally {
      setEmployeesLoading(false);
    }
  }, [
    apiFetch,
    employeeRoleFilter,
    employeeSearch,
    employeeStatusFilter,
  ]);

  useEffect(() => {
    if (activeTab !== "employees") return undefined;

    const timer = window.setTimeout(loadEmployees, 250);

    return () => window.clearTimeout(timer);
  }, [
    activeTab,
    employeeRoleFilter,
    employeeSearch,
    employeeStatusFilter,
    loadEmployees,
  ]);

  const switchTab = (tab) => {
    setActiveTab(tab);

    if (tab === "tasks") {
      loadTasks();
    }
  };

  const addMessage = (role, text) => {
    setMessages((current) => [
      ...current,
      {
        id: `${Date.now()}-${Math.random()}`,
        role,
        text: text || "Done.",
        feedback: null,
      },
    ]);
  };

  const applyConversation = (data, shouldSpeak = false) => {
    const reply = data.reply || data.question || data.message || "Done.";

    setStatus(data.status || null);
    setMissingFields(data.missing_fields || []);
    setRequiredFields(data.required_fields || DEFAULT_REQUIRED_FIELDS);

    // The task of the answer, when there is one. A "not found" answer has no
    // record at all, and the panel is only shown for a real task.
    const task = data.task && typeof data.task === "object" ? data.task : null;

    if (data.status === "success") {
      setStage(null);
      setDraft(null);
      setSavedTask(task);
      setSavedMessage(data.message || "");
    } else {
      // The stage and the draft always come from the answer. A stale stage
      // would send the next message into the wrong flow: a new task could
      // be answered with a lookup of an existing task.
      setStage(data.stage ?? null);
      setDraft(data.draft ?? null);
      setSavedTask(null);
    }

    // Persist trusted notification context from backend when present.
    if (data.notification_context?.notification_id) {
      activeNotificationIdRef.current = data.notification_context.notification_id;
    }

    addMessage("assistant", reply);

    if (shouldSpeak) {
      speak(reply);
    }
  };

  const submitMessage = async (message, options = {}) => {
    if (!message || loading) return;
    const notificationId =
      options.notification_id ?? activeNotificationIdRef.current ?? undefined;
    const requestStage = options.stage !== undefined ? options.stage : stage;
    const requestDraft = options.draft !== undefined ? options.draft : draft;

    addMessage("user", message);
    setLoading(true);
    setError("");

    try {
      const payload = {
        message,
        stage: requestStage,
        draft: requestDraft,
        conversation_id: conversationId,
        notification_id: notificationId,
      };
      console.info("chat_request", {
        notification_id: notificationId,
        stage: requestStage,
        has_draft: !!requestDraft,
      });
      const response = await apiFetch(CHAT_API, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || "Chat processing failed.");
      }

      console.info("chat_response", {
        notification_id: notificationId,
        status: data.status,
        intent: data.intent,
        stage: data.stage,
        has_notification_context: !!data.notification_context,
      });
      applyConversation(data);
    } catch (err) {
      setError(err.message);
      addMessage(
        "assistant",
        `Sorry, something went wrong: ${err.message}`
      );
    } finally {
      setLoading(false);
    }
  };

  const submitFeedback = async (messageId, feedback) => {
    setError("");

    setMessages((current) =>
      current.map((message) =>
        message.id === messageId ? { ...message, feedback } : message
      )
    );

    try {
      const response = await apiFetch(FEEDBACK_API, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          conversation_id: conversationId,
          feedback,
        }),
      });

      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        throw new Error(data.detail || "Unable to save feedback.");
      }
    } catch (err) {
      setMessages((current) =>
        current.map((message) =>
          message.id === messageId ? { ...message, feedback: null } : message
        )
      );
      setError(err.message);
    }
  };

  const sendTextMessage = () => {
    const message = input.trim();
    if (!message || loading) return;

    // If user explicitly starts a new task, clear notification follow-up anchor.
    if (/^\s*(create|add|make)\s+(a\s+)?(new\s+)?task\b/i.test(message)) {
      activeNotificationIdRef.current = null;
    }
    setInput("");
    submitMessage(message);
  };

  const handleSubmit = (event) => {
    event.preventDefault();
    sendTextMessage();
  };

  const handleKeyDown = (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendTextMessage();
    }
  };

  const startNewTask = () => {
    setActiveTab("assistant");
    setMessages([
      {
        id: `welcome-${Date.now()}`,
        role: "assistant",
        text: welcomeMessage(auth.employee),
      },
    ]);
    setInput("");
    setStage(null);
    setDraft(null);
    setStatus(null);
    setMissingFields([]);
    setRequiredFields(DEFAULT_REQUIRED_FIELDS);
    setSavedTask(null);
    setError("");
    setAudio(null);
    setAudioUrl("");
    activeNotificationIdRef.current = null;
    // A new conversation starts with a new id, so the memory of the older
    // conversation (the active task) is not reused.
    setConversationId(newConversationId());
  };

  const startRecording = async () => {
    try {
      setError("");
      if (audioUrl) URL.revokeObjectURL(audioUrl);
      setAudio(null);
      setAudioUrl("");

      const stream = await navigator.mediaDevices.getUserMedia({
        audio: true,
      });

      const mimeType = [
        "audio/webm;codecs=opus",
        "audio/webm",
        "audio/ogg;codecs=opus",
      ].find((type) => MediaRecorder.isTypeSupported(type));

      const mediaRecorder = mimeType
        ? new MediaRecorder(stream, { mimeType })
        : new MediaRecorder(stream);

      const chunks = [];

      mediaRecorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          chunks.push(event.data);
        }
      };

      mediaRecorder.onstop = () => {
        const blob = new Blob(chunks, {
          type: mediaRecorder.mimeType,
        });

        setAudio(blob);
        setAudioUrl(URL.createObjectURL(blob));
        stream.getTracks().forEach((track) => track.stop());
      };

      mediaRecorder.start();

      setRecorder(mediaRecorder);
      setRecording(true);
    } catch (err) {
      setError("Unable to access microphone: " + err.message);
    }
  };

  const stopRecording = () => {
    if (recorder && recorder.state === "recording") {
      recorder.stop();
    }

    setRecording(false);
  };

  const sendAudio = async () => {
    if (!audio || loading) return;

    const extension = audio.type.includes("ogg")
      ? "ogg"
      : audio.type.includes("mp4")
        ? "mp4"
        : "webm";

    const formData = new FormData();
    formData.append("file", audio, `recording.${extension}`);

    const conversationState = {};
    if (stage) conversationState.stage = stage;
    if (draft) conversationState.draft = draft;
    conversationState.conversation_id = conversationId;

    if (Object.keys(conversationState).length > 0) {
      formData.append("state", JSON.stringify(conversationState));
    }

    setLoading(true);
    setError("");

    try {
      const response = await apiFetch(VOICE_TURN_API, {
        method: "POST",
        body: formData,
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || "Transcription failed.");
      }

      addMessage("user", `🎤 ${data.transcription}`);

      applyConversation(
        {
          ...data.result,
          reply:
            data.reply ||
            data.result?.reply ||
            data.result?.question ||
            data.result?.message,
        },
        true
      );

      setAudio(null);
      if (audioUrl) URL.revokeObjectURL(audioUrl);
      setAudioUrl("");
    } catch (err) {
      setError(err.message);
      addMessage(
        "assistant",
        `Sorry, I could not process that voice note: ${err.message}`
      );
    } finally {
      setLoading(false);
    }
  };

  const collectedCount = requiredFields.filter(
    (field) => draft?.[field] !== null && draft?.[field] !== undefined
  ).length;

  const quickReplies = (() => {
    if (stage === "confirm_fields") {
      return [
        { label: "Yes, save it", value: "yes" },
        { label: "Change something", value: "change" },
      ];
    }

    if (stage === "confirm_reminder") {
      return [
        { label: "Yes, correct", value: "yes" },
        { label: "Change reminder", value: "change" },
        { label: "No reminder needed", value: "no reminder" },
      ];
    }

    if (stage === "reminder") {
      return [
        { label: "30 minutes before deadline", value: "30 minutes before the deadline" },
        { label: "1 hour before deadline", value: "1 hour before deadline" },
        { label: "No reminder needed", value: "no reminder" },
      ];
    }

    if (stage === "confirm_update") {
      return [
        { label: "Yes, apply the change", value: "yes" },
        { label: "Change the value", value: "change" },
      ];
    }

    if (stage === "confirm_delete") {
      return [
        { label: "CONFIRM DELETE", value: "CONFIRM DELETE" },
        { label: "Keep the task", value: "no" },
      ];
    }

    if (stage === "select_task") {
      const candidates = draft?.candidates || [];

      if (candidates.length === 0) {
        return [{ label: "Tell me the task ID", value: "task " }];
      }

      return [
        { label: "The first one", value: "the first one" },
        { label: "The second one", value: "the second one" },
      ];
    }

    return [];
  })();


  const employee = auth.employee;
  const canReadAllTasks = hasReadAllTasks(employee);
  const canManageEmployeesFlag = canManageEmployees(employee);
  const canCreateTasksFlag = canCreateTasks(employee);
  const canDeleteTasksFlag = canDeleteTasks(employee);
  const employeeRoleOptions = assignableRoles(employee, employeeRoles);

  const openAddEmployee = () => {
    setEmployeeDialog({ mode: "add", employee: null });
    setEmployeeForm({ name: "", email: "", role: "EMPLOYEE" });
    setEmployeesError("");
    setEmployeeMessage("");
  };

  const openViewEmployee = (target) => {
    setEmployeeDialog({ mode: "view", employee: target });
    setEmployeesError("");
  };

  const openEditEmployee = (target) => {
    setEmployeeDialog({ mode: "edit", employee: target });
    setEmployeeForm({
      name: target.name,
      email: target.email,
      role: target.role,
    });
    setEmployeesError("");
    setEmployeeMessage("");
  };

  const closeEmployeeDialog = () => {
    if (employeeSaving) return;
    setEmployeeDialog(null);
    setEmployeeConfirm(null);
  };

  const runEmployeeMutation = async ({
    url,
    method = "POST",
    body,
    successMessage,
    closeDialog = true,
  }) => {
    setEmployeeSaving(true);
    setEmployeesError("");
    setEmployeeMessage("");

    try {
      const response = await apiFetch(url, {
        method,
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        throw new Error(
          data.detail || data.message || "The employee action failed."
        );
      }

      if (data.employee && data.employee.id === auth.employee?.id) {
        setAuth((current) => ({
          ...current,
          employee: {
            ...current.employee,
            name: data.employee.name,
            role: data.employee.role,
          },
        }));
      }

      setEmployeeMessage(successMessage);
      setEmployeeConfirm(null);

      if (closeDialog) {
        setEmployeeDialog(null);
      }

      await loadEmployees();
      return data;
    } catch (err) {
      setEmployeesError(err.message);
      return null;
    } finally {
      setEmployeeSaving(false);
    }
  };

  const submitEmployeeForm = async (event) => {
    event.preventDefault();

    if (!employeeDialog || employeeSaving) return;

    if (employeeDialog.mode === "add") {
      await runEmployeeMutation({
        url: EMPLOYEES_API,
        method: "POST",
        body: employeeForm,
        successMessage: `${employeeForm.name.trim()} was added.`,
      });
      return;
    }

    if (employeeDialog.mode === "edit") {
      const payload = {};
      const nextName = employeeForm.name.trim();

      if (nextName !== employeeDialog.employee.name) {
        payload.name = nextName;
      }

      if (employeeForm.role !== employeeDialog.employee.role) {
        payload.role = employeeForm.role;
      }

      if (Object.keys(payload).length === 0) {
        setEmployeeMessage("No changes to save.");
        setEmployeeDialog(null);
        return;
      }

      if (payload.role) {
        setEmployeeForm((current) => ({ ...current, name: nextName }));
        setEmployeeConfirm({
          kind: "update",
          employee: employeeDialog.employee,
          payload,
        });
        return;
      }

      await runEmployeeMutation({
        url: `${EMPLOYEES_API}/${employeeDialog.employee.id}`,
        method: "PATCH",
        body: payload,
        successMessage: "Employee details updated.",
      });
    }
  };

  const requestEmployeeAction = (kind, target) => {
    setEmployeesError("");
    setEmployeeConfirm({ kind, employee: target, payload: null });
  };

  const confirmEmployeeAction = async () => {
    if (!employeeConfirm) return;

    const { kind, employee: target, payload } = employeeConfirm;

    if (kind === "deactivate") {
      await runEmployeeMutation({
        url: `${EMPLOYEES_API}/${target.id}/deactivate`,
        successMessage: `${target.name} was deactivated. Their sessions were revoked.`,
      });
      return;
    }

    if (kind === "activate") {
      await runEmployeeMutation({
        url: `${EMPLOYEES_API}/${target.id}/activate`,
        successMessage: `${target.name} was activated.`,
      });
      return;
    }

    if (kind === "revoke-sessions") {
      await runEmployeeMutation({
        url: `${EMPLOYEES_API}/${target.id}/revoke-sessions`,
        successMessage: `All sessions for ${target.name} were revoked.`,
      });
      return;
    }

    if (kind === "update") {
      await runEmployeeMutation({
        url: `${EMPLOYEES_API}/${target.id}`,
        method: "PATCH",
        body: payload,
        successMessage: "Employee details updated.",
      });
    }
  };

  const employeeConfirmContent = (() => {
    if (!employeeConfirm) return null;

    const target = employeeConfirm.employee;

    if (employeeConfirm.kind === "deactivate") {
      return {
        title: `Deactivate ${target.name}?`,
        body: "Their access is revoked immediately and every active refresh session is ended.",
        action: "Deactivate",
        danger: true,
      };
    }

    if (employeeConfirm.kind === "activate") {
      return {
        title: `Activate ${target.name}?`,
        body: "They will be able to sign in again with their provisioned Google account.",
        action: "Activate",
        danger: false,
      };
    }

    if (employeeConfirm.kind === "revoke-sessions") {
      return {
        title: `Revoke all sessions for ${target.name}?`,
        body: "Every signed-in device is signed out immediately. The employee can sign in again.",
        action: "Revoke sessions",
        danger: true,
      };
    }

    return {
      title: `Change role for ${target.name}?`,
      body: `The role will change to ${roleLabel(
        employeeConfirm.payload?.role
      )}. Permissions take effect on their next request.`,
      action: "Change role",
      danger: true,
    };
  })();

  const taskStats = {
    total: tasks.length,
    inProgress: tasks.filter((task) => task.status === "IN_PROGRESS").length,
    completed: tasks.filter((task) => task.status === "COMPLETED").length,
    overdue: tasks.filter(isTaskOverdue).length,
  };

  const renderEmployeesTab = () => (
    <main className="tasks-layout employees-layout">
      <section className="tasks-panel">
        <div className="tasks-toolbar">
          <div>
            <p className="eyebrow">Administration</p>
            <h2>Employee Management</h2>
            <p className="tasks-subtitle">
              Provision work accounts, assign roles and control access.
            </p>
          </div>

          <div className="tasks-toolbar-actions">
            <button
              type="button"
              className="refresh-button"
              onClick={loadEmployees}
              disabled={employeesLoading}
            >
              {employeesLoading ? "Refreshing…" : "↻ Refresh"}
            </button>
            <button
              type="button"
              className="ghost-button"
              onClick={openAddEmployee}
            >
              + Add employee
            </button>
          </div>
        </div>

        {employeeMessage && (
          <p className="employee-success" role="status">
            {employeeMessage}
          </p>
        )}
        {employeesError && <p className="chat-error">{employeesError}</p>}

        <div className="task-stats employee-stats">
          <div className="task-stat total">
            <span>Total employees</span>
            <strong>{employeeSummary.total}</strong>
          </div>
          <div className="task-stat active">
            <span>Active</span>
            <strong>{employeeSummary.active}</strong>
          </div>
          <div className="task-stat done">
            <span>Inactive</span>
            <strong>{employeeSummary.inactive}</strong>
          </div>
          <div className="task-stat overdue">
            <span>Not signed in yet</span>
            <strong>{employeeSummary.not_logged_in}</strong>
          </div>
        </div>

        <div className="employee-filters">
          <label className="employee-search">
            <span>Search</span>
            <input
              type="search"
              value={employeeSearch}
              onChange={(event) => setEmployeeSearch(event.target.value)}
              placeholder="Search by name or email"
            />
          </label>

          <label className="employee-search">
            <span>Role</span>
            <select
              value={employeeRoleFilter}
              onChange={(event) => setEmployeeRoleFilter(event.target.value)}
            >
              <option value="">All roles</option>
              {employeeRoles.map((role) => (
                <option key={role} value={role}>
                  {roleLabel(role)}
                </option>
              ))}
            </select>
          </label>

          <label className="employee-search">
            <span>Status</span>
            <select
              value={employeeStatusFilter}
              onChange={(event) => setEmployeeStatusFilter(event.target.value)}
            >
              <option value="">All statuses</option>
              <option value="active">Active</option>
              <option value="inactive">Inactive</option>
              <option value="not_logged_in">Not signed in yet</option>
            </select>
          </label>

          {(employeeSearch ||
            employeeRoleFilter ||
            employeeStatusFilter) && (
            <button
              type="button"
              className="employee-clear"
              onClick={() => {
                setEmployeeSearch("");
                setEmployeeRoleFilter("");
                setEmployeeStatusFilter("");
              }}
            >
              Clear filters
            </button>
          )}
        </div>

        <div className="tasks-table-wrap">
          {employeesLoading && employees.length === 0 ? (
            <p className="tasks-empty">Loading employees…</p>
          ) : employees.length === 0 ? (
            <p className="tasks-empty">
              No employees match the current filters.
            </p>
          ) : (
            <table className="tasks-table employees-table">
              <thead>
                <tr>
                  <th>Employee</th>
                  <th>Role</th>
                  <th>Status</th>
                  <th>Login</th>
                  <th>Added</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {employees.map((row) => (
                  <tr key={row.id}>
                    <td className="title-cell">
                      <strong>{row.name}</strong>
                      <span className="cell-muted">{row.email}</span>
                    </td>
                    <td>
                      <span className={`role-badge ${statusClass(row.role)}`}>
                        {roleLabel(row.role)}
                      </span>
                    </td>
                    <td>
                      <span
                        className={`status-badge ${
                          row.is_active ? "completed" : "pending"
                        }`}
                      >
                        {row.is_active ? "Active" : "Inactive"}
                      </span>
                    </td>
                    <td>
                      <span
                        className={`login-state ${
                          row.has_logged_in ? "registered" : "unregistered"
                        }`}
                      >
                        {row.has_logged_in
                          ? "Google linked"
                          : "Not signed in yet"}
                      </span>
                    </td>
                    <td>{formatDateTime(row.created_at)}</td>
                    <td>
                      <div className="employee-actions">
                        <button
                          type="button"
                          className="employee-action"
                          onClick={() => openViewEmployee(row)}
                        >
                          View
                        </button>
                        {canManageEmployee(employee, row) && (
                          <>
                            <button
                              type="button"
                              className="employee-action"
                              onClick={() => openEditEmployee(row)}
                            >
                              Edit
                            </button>
                            <button
                              type="button"
                              className={`employee-action ${
                                row.is_active ? "danger" : ""
                              }`}
                              onClick={() =>
                                requestEmployeeAction(
                                  row.is_active ? "deactivate" : "activate",
                                  row
                                )
                              }
                            >
                              {row.is_active ? "Deactivate" : "Activate"}
                            </button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>
    </main>
  );

  const renderTasksTab = () => (
    <main className="tasks-layout">
      <section className="tasks-panel">
        <div className="tasks-toolbar">
          <div>
            <p className="eyebrow">Workspace</p>
            <h2>{canReadAllTasks ? "All Tasks" : "My Tasks"}</h2>
            <p className="tasks-subtitle">
              {canReadAllTasks
                ? "Every task in your workspace, with full scheduling details."
                : "Tasks assigned to your employee account."}
            </p>
          </div>

          <div className="tasks-toolbar-actions">
            {tasksFetchedAt && (
              <span className="last-updated">Updated {tasksFetchedAt}</span>
            )}
            <button
              type="button"
              className="refresh-button"
              onClick={loadTasks}
              disabled={tasksLoading}
            >
              {tasksLoading ? "Refreshing…" : "↻ Refresh"}
            </button>
          </div>
        </div>

        <div className="task-stats">
          <div className="task-stat total">
            <span>Total tasks</span>
            <strong>{taskStats.total}</strong>
          </div>
          <div className="task-stat active">
            <span>In progress</span>
            <strong>{taskStats.inProgress}</strong>
          </div>
          <div className="task-stat done">
            <span>Completed</span>
            <strong>{taskStats.completed}</strong>
          </div>
          <div className="task-stat overdue">
            <span>Overdue</span>
            <strong>{taskStats.overdue}</strong>
          </div>
        </div>

        {tasksError && <p className="chat-error">{tasksError}</p>}
        {taskMessage && (
          <p className="employee-success" role="status">
            {taskMessage}
          </p>
        )}

        <div className="tasks-table-wrap">
          {tasksLoading && tasks.length === 0 ? (
            <p className="tasks-empty">Loading tasks…</p>
          ) : tasks.length === 0 ? (
            <p className="tasks-empty">
              No tasks yet. Create one from the AI Assistant tab.
            </p>
          ) : (
            <table className="tasks-table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Task</th>
                  <th>Assignee</th>
                  <th>Assigned</th>
                  <th>Deadline</th>
                  <th>Reminder</th>
                  <th>Status</th>
                  <th>Progress</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((task) => {
                  const progress = Math.min(
                    100,
                    Math.max(0, Number(task.progress) || 0)
                  );
                  const canEditProgress = canUpdateProgress(employee, task);
                  const rowBusy = taskActionId === task.id;

                  return (
                    <tr
                      key={task.id}
                      id={`task-row-${task.id}`}
                      className={[
                        isTaskOverdue(task) ? "overdue-row" : "",
                        highlightedTaskId === task.id
                          ? "notification-highlight"
                          : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                    >
                      <td className="id-cell">#{task.id}</td>
                      <td className="title-cell">
                        <strong>{task.task_title || "Untitled task"}</strong>
                        {task.task_description && (
                          <span className="cell-muted">
                            {task.task_description}
                          </span>
                        )}
                      </td>
                      <td>
                        {task.assigned_to || (
                          <span className="muted-cell">Unassigned</span>
                        )}
                      </td>
                      <td>
                        <DateCell
                          date={task.assigned_date}
                          time={task.assigned_time}
                        />
                      </td>
                      <td>
                        <DateCell
                          date={task.deadline_date}
                          time={task.deadline_time}
                        />
                      </td>
                      <td>
                        <DateCell
                          date={task.reminder_date}
                          time={task.reminder_time}
                        />
                      </td>
                      <td>
                        <span
                          className={`status-badge ${statusClass(task.status)}`}
                        >
                          {(task.status || "PENDING").replaceAll("_", " ")}
                        </span>
                      </td>
                      <td>
                        <div className="progress-cell">
                          <div className="progress-track">
                            <div
                              className="progress-fill"
                              style={{ width: `${progress}%` }}
                            />
                          </div>
                          <span className="progress-value">{progress}%</span>
                        </div>
                      </td>
                      <td>
                        <div className="task-row-actions">
                          {canEditProgress && (
                            <label className="progress-editor">
                              <span className="sr-only">
                                Progress for task {task.id}
                              </span>
                              <input
                                type="number"
                                min="0"
                                max="100"
                                step="1"
                                value={progressDrafts[task.id] ?? progress}
                                onChange={(event) =>
                                  setProgressDrafts((current) => ({
                                    ...current,
                                    [task.id]: event.target.value,
                                  }))
                                }
                                disabled={rowBusy}
                              />
                              <button
                                type="button"
                                className="employee-action"
                                onClick={() => openEditTask(task)}
                                disabled={rowBusy}
                              >
                                Edit
                              </button>
                              <button
                                type="button"
                                className="employee-action"
                                onClick={() => saveTaskProgress(task)}
                                disabled={rowBusy}
                              >
                                {rowBusy ? "Saving…" : "Save"}
                              </button>
                            </label>
                          )}
                          {canDeleteTasksFlag && (
                            <button
                              type="button"
                              className="employee-action danger"
                              onClick={() => requestDeleteTask(task)}
                              disabled={rowBusy}
                            >
                              Delete
                            </button>
                          )}
                          {!canEditProgress && !canDeleteTasksFlag && (
                            <span className="muted-cell">View only</span>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </section>
    </main>
  );

  if (auth.status === "loading") {
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <p className="eyebrow">Task Flow Automation</p>
          <h1>Checking your session</h1>
          <p className="auth-copy">Restoring your secure workspace…</p>
        </div>
      </div>
    );
  }

  if (auth.status !== "authenticated" || !employee) {
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <p className="eyebrow">Task Flow Automation</p>
          <h1>Sign in to your workspace</h1>
          <p className="auth-copy">
            Use your Google work account. Access is limited to employees who
            have been provisioned by an administrator.
          </p>

          {authError && <p className="auth-error">{authError}</p>}

          <button
            type="button"
            className="google-button"
            onClick={startLogin}
            disabled={authBusy}
          >
            {authBusy ? "Opening Google…" : "Continue with Google"}
          </button>

          <p className="auth-footnote">
            You will be redirected to Google and returned to this page after
            sign-in.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div
      className={`app ${
        activeTab === "tasks"
          ? "tasks-view"
          : activeTab === "employees"
            ? "employees-view"
            : ""
      }`}
    >
      <header className="app-header">
        <div>
          <p className="eyebrow">Task Flow Automation</p>
          <h1>Task Assistant</h1>
          <p className="subtitle">
            Create a task through a natural back-and-forth conversation.
          </p>
        </div>

        <div className="header-actions">
          <div className="notification-menu" ref={notificationMenuRef}>
            <button
              type="button"
              className={`notification-bell ${
                notificationsOpen ? "active" : ""
              }`}
              aria-label={`Notifications, ${unreadCount} unread`}
              aria-expanded={notificationsOpen}
              onClick={toggleNotifications}
            >
              <BellIcon />
              {unreadCount > 0 && (
                <span className="notification-badge" aria-hidden="true">
                  {unreadBadgeLabel(unreadCount)}
                </span>
              )}
            </button>

            {notificationsOpen && (
              <div className="notification-panel" role="dialog" aria-label="Notifications">
                <div className="notification-panel-head">
                  <div>
                    <p className="eyebrow">Reminders</p>
                    <h2>Notifications</h2>
                  </div>
                  <div className="notification-head-actions">
                    <button
                      type="button"
                      className="notification-mark-all"
                      onClick={markAllNotificationsRead}
                      disabled={unreadCount === 0 || notificationActionId === "all"}
                    >
                      {notificationActionId === "all" ? "Saving…" : "Mark all read"}
                    </button>
                    <button
                      type="button"
                      className="notification-mark-all danger"
                      onClick={deleteAllNotifications}
                      disabled={notifications.length === 0 || notificationActionId === "delete-all"}
                    >
                      {notificationActionId === "delete-all" ? "Deleting…" : "Delete all"}
                    </button>
                  </div>
                </div>

                {notificationsError && (
                  <p className="chat-error notification-error">
                    {notificationsError}
                  </p>
                )}

                <div className="notification-list">
                  {notificationsLoading && notifications.length === 0 ? (
                    <p className="notification-empty">Loading notifications…</p>
                  ) : notifications.length === 0 ? (
                    <div className="notification-empty">
                      <strong>No notifications yet</strong>
                      <span>
                        Task reminders will appear here when they are due.
                      </span>
                    </div>
                  ) : (
                    notifications.map((item) => {
                      const payload = item.payload || {};
                      const actionable =
                        payload.has_missing_fields &&
                        (Array.isArray(payload.actions) ? payload.actions.length > 0 : true) &&
                        !["collecting", "completed", "dismissed"].includes(
                          String(payload.followup_status || "pending").toLowerCase()
                        );
                      return (
                        <article
                          key={item.id}
                          className={`notification-item ${item.is_read ? "" : "unread"}`}
                        >
                          <button
                            type="button"
                            className="notification-open"
                            onClick={() => openNotification(item)}
                            disabled={notificationActionId === item.id}
                          >
                            <span className="notification-dot" aria-hidden="true" />
                            <span className="notification-copy">
                              <strong>
                                {item.title}
                                {["HIGH", "CRITICAL"].includes(
                                  String(item.priority || "").toUpperCase()
                                ) && (
                                  <span className={`priority-badge ${String(item.priority).toLowerCase()}`}>
                                    {item.priority}
                                  </span>
                                )}
                              </strong>
                              <span className="notification-message">{item.message}</span>
                              {Array.isArray(payload.missing_fields) && payload.missing_fields.length > 0 && (
                                <span className="notification-missing">
                                  Missing: {payload.missing_fields.map((f) => FIELD_META[f]?.label || f).join(", ")}
                                </span>
                              )}
                              <small>
                                {formatDateTime(item.created_at)}
                                {item.delivery_status === "NO_SUBSCRIPTION" ||
                                item.delivery_status === "FAILED" ||
                                item.delivery_status === "PARTIAL"
                                  ? " · In-app only"
                                  : ""}
                              </small>
                            </span>
                          </button>
                          <div className="notification-actions">
                            {actionable && (
                              <>
                                <button
                                  type="button"
                                  className="notification-action primary"
                                  onClick={() => respondToNotification(item, "accept")}
                                  disabled={notificationActionId === item.id}
                                >
                                  Yes, complete details
                                </button>
                                <button
                                  type="button"
                                  className="notification-action"
                                  onClick={() => respondToNotification(item, "dismiss")}
                                  disabled={notificationActionId === item.id}
                                >
                                  No, not now
                                </button>
                              </>
                            )}
                            {!item.is_read && !actionable && (
                              <button
                                type="button"
                                className="notification-read"
                                onClick={() => markNotificationRead(item.id)}
                                disabled={notificationActionId === item.id}
                              >
                                Mark read
                              </button>
                            )}
                            <button
                              type="button"
                              className="notification-read danger"
                              onClick={() => deleteNotification(item.id)}
                              disabled={notificationActionId === `delete-${item.id}`}
                              aria-label={`Delete notification ${item.id}`}
                            >
                              {notificationActionId === `delete-${item.id}` ? "Deleting…" : "Delete"}
                            </button>
                          </div>
                        </article>
                      );
                    })
                  )}
                </div>

                <details className="notification-settings">
                  <summary>
                    <span>Alerts &amp; devices</span>
                    <small>Push, voice, alarm and timezone</small>
                  </summary>
                  <div className="notification-settings-body">
                  <div className="push-settings">
                    <div>
                      <strong>Device push</strong>
                      <span>
                        Browser push works in background when supported; voice
                        and alarm alerts run while this app is open.
                      </span>
                    </div>
                    {pushState === "subscribed" ? (
                      <button
                        type="button"
                        className="notification-setting-button"
                        onClick={disablePushNotifications}
                        disabled={pushBusy}
                      >
                        {pushBusy ? "Working…" : "Disable"}
                      </button>
                    ) : pushState === "unsupported" ? (
                      <small>This browser does not support Web Push.</small>
                    ) : pushState === "denied" ? (
                      <small>Blocked in browser settings.</small>
                    ) : (
                      <button
                        type="button"
                        className="notification-setting-button primary"
                        onClick={enablePushNotifications}
                        disabled={pushBusy || !pushConfigured}
                      >
                        {pushBusy ? "Enabling…" : "Enable on this device"}
                      </button>
                    )}
                  </div>

                  <div className="attention-settings">
                    <div className="attention-head">
                      <strong>Spoken reminders</strong>
                      <button
                        type="button"
                        className="notification-setting-button primary"
                        onClick={enableAttentionAlerts}
                      >
                        {alertPrefs.attentionEnabled
                          ? "Re-enable audio"
                          : "Enable voice & alarm"}
                      </button>
                    </div>
                    <small>
                      When this tab is open, the assistant can play an alarm
                      and speak reminders like “Hey {employee?.name?.split(" ")[0] || "there"},
                      this is a reminder for you…”. Background/closed pages
                      still rely on browser Web Push; speech/audio cannot run
                      without an open, permitted tab.
                    </small>
                    <div className="attention-toggles">
                      <label>
                        <input
                          type="checkbox"
                          checked={alertPrefs.voiceEnabled}
                          onChange={(event) =>
                            updateAlertPrefs({ voiceEnabled: event.target.checked })
                          }
                        />
                        Read task reminders aloud
                      </label>
                      <label>
                        <input
                          type="checkbox"
                          checked={alertPrefs.soundEnabled}
                          onChange={(event) =>
                            updateAlertPrefs({ soundEnabled: event.target.checked })
                          }
                        />
                        Play alarm sound
                      </label>
                    </div>
                    <label className="attention-slider">
                      <span>Volume {Math.round(alertPrefs.volume * 100)}%</span>
                      <input
                        type="range"
                        min="0"
                        max="1"
                        step="0.05"
                        value={alertPrefs.volume}
                        onChange={(event) =>
                          updateAlertPrefs({ volume: Number(event.target.value) })
                        }
                      />
                    </label>
                    <label className="attention-slider">
                      <span>Speech rate {Number(alertPrefs.rate).toFixed(2)}x</span>
                      <input
                        type="range"
                        min="0.6"
                        max="1.4"
                        step="0.05"
                        value={alertPrefs.rate}
                        onChange={(event) =>
                          updateAlertPrefs({ rate: Number(event.target.value) })
                        }
                      />
                    </label>
                    <label className="attention-slider">
                      <span>Speech pitch {Number(alertPrefs.pitch).toFixed(2)}</span>
                      <input
                        type="range"
                        min="0.7"
                        max="1.4"
                        step="0.05"
                        value={alertPrefs.pitch}
                        onChange={(event) =>
                          updateAlertPrefs({ pitch: Number(event.target.value) })
                        }
                      />
                    </label>
                    <div className="attention-tests">
                      <button
                        type="button"
                        className="notification-setting-button"
                        onClick={testAttentionAlerts}
                        disabled={!alertPrefs.attentionEnabled}
                      >
                        Test spoken reminder
                      </button>
                      <button
                        type="button"
                        className="notification-setting-button"
                        onClick={() => alertControllerRef.current?.stopSpeech()}
                      >
                        Stop speech
                      </button>
                    </div>
                    {alertStatus && (
                      <p className="push-message" role="status">
                        {alertStatus}
                      </p>
                    )}
                  </div>

                  <form
                    className="timezone-settings"
                    onSubmit={saveNotificationTimezone}
                  >
                    <label>
                      <span>Reminder timezone</span>
                      <input
                        type="text"
                        value={timezoneDraft}
                        onChange={(event) =>
                          setTimezoneDraft(event.target.value)
                        }
                        placeholder="Asia/Kolkata"
                        disabled={timezoneSaving}
                      />
                    </label>
                    <button
                      type="submit"
                      className="notification-setting-button"
                      disabled={
                        timezoneSaving ||
                        !timezoneDraft.trim() ||
                        timezoneDraft.trim() === notificationTimezone
                      }
                    >
                      {timezoneSaving ? "Saving…" : "Save"}
                    </button>
                  </form>

                  {pushMessage && (
                    <p className="push-message" role="status">
                      {pushMessage}
                    </p>
                  )}
                  {!pushConfigured && pushState !== "unsupported" && (
                    <p className="push-message">
                      Push is not configured on this server; in-app reminders
                      still work.
                    </p>
                  )}
                  </div>
                </details>
              </div>
            )}
          </div>

          <div className="profile-menu">
            <button
              type="button"
              className="profile-button"
              aria-expanded={profileOpen}
              onClick={() => setProfileOpen((open) => !open)}
            >
              <span className="profile-avatar">
                {(employee.name || employee.email || "?").slice(0, 1).toUpperCase()}
              </span>
              <span className="profile-summary">
                <strong>{employee.name || employee.email}</strong>
                <small>{roleLabel(employee.role)}</small>
              </span>
            </button>

            {profileOpen && (
              <div className="profile-card">
                <p className="profile-name">{employee.name}</p>
                <p className="profile-email">{employee.email}</p>
                <dl className="profile-details">
                  <div>
                    <dt>Role</dt>
                    <dd>{roleLabel(employee.role)}</dd>
                  </div>
                  <div>
                    <dt>Scope</dt>
                    <dd>
                      {canReadAllTasks ? "All workspace tasks" : "Your tasks"}
                    </dd>
                  </div>
                </dl>
                <button
                  type="button"
                  className="profile-action"
                  onClick={() => signOut(false)}
                  disabled={authBusy}
                >
                  Sign out
                </button>
                <button
                  type="button"
                  className="profile-action danger"
                  onClick={() => signOut(true)}
                  disabled={authBusy}
                >
                  Sign out of all devices
                </button>
              </div>
            )}
          </div>

          {canCreateTasksFlag && (
            <button
              type="button"
              className="ghost-button"
              onClick={startNewTask}
            >
              + New task
            </button>
          )}
        </div>
      </header>

      <nav className="tab-bar" role="tablist" aria-label="Task views">
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "assistant"}
          className={`tab-button ${activeTab === "assistant" ? "active" : ""}`}
          onClick={() => switchTab("assistant")}
        >
          AI Assistant
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "tasks"}
          className={`tab-button ${activeTab === "tasks" ? "active" : ""}`}
          onClick={() => switchTab("tasks")}
        >
          {canReadAllTasks ? "All Tasks" : "My Tasks"}
          {tasks.length > 0 && (
            <span className="tab-badge">{tasks.length}</span>
          )}
        </button>
        {canManageEmployeesFlag && (
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "employees"}
            className={`tab-button ${
              activeTab === "employees" ? "active" : ""
            }`}
            onClick={() => switchTab("employees")}
          >
            Employees
          </button>
        )}
      </nav>

      {activeTab === "assistant" ? (
        <main className="chat-layout">
        <section className="chat-card">
          {draft && (
            <div className="field-progress">
              <div className="field-progress-head">
                <span>Collected details</span>
                <strong>
                  {collectedCount}/{requiredFields.length} required
                </strong>
              </div>
              <div className="field-chips">
                {FIELD_ORDER.map((field) => {
                  const filled =
                    draft?.[field] !== null && draft?.[field] !== undefined;
                  const isRequired = requiredFields.includes(field);

                  return (
                    <span
                      key={field}
                      className={`field-chip ${
                        filled
                          ? "filled"
                          : isRequired
                            ? "missing"
                            : "optional"
                      }`}
                      title={isRequired ? "Required" : "Optional"}
                    >
                      {FIELD_META[field].label}
                      {isRequired ? "" : " (optional)"}
                    </span>
                  );
                })}
              </div>
            </div>
          )}

          <div className="chat-window">
            {messages.map((message) => (
              <div
                key={message.id}
                className={`bubble-row ${message.role}`}
              >
                <div className="bubble-content">
                  <div className={`bubble ${message.role}`}>{message.text}</div>

                  {message.role === "assistant" && (
                    <div
                      className="feedback-actions"
                      aria-label="Rate this response"
                    >
                      <button
                        type="button"
                        className={`feedback-button ${
                          message.feedback === true ? "active" : ""
                        }`}
                        aria-label="Thumbs up"
                        aria-pressed={message.feedback === true}
                        title="Thumbs up"
                        onClick={() => submitFeedback(message.id, true)}
                      >
                        <ThumbsUpIcon />
                      </button>
                      <button
                        type="button"
                        className={`feedback-button ${
                          message.feedback === false ? "active" : ""
                        }`}
                        aria-label="Thumbs down"
                        aria-pressed={message.feedback === false}
                        title="Thumbs down"
                        onClick={() => submitFeedback(message.id, false)}
                      >
                        <ThumbsDownIcon />
                      </button>
                    </div>
                  )}
                </div>
              </div>
            ))}

            {loading && (
              <div className="bubble-row assistant">
                <div className="bubble assistant typing">
                  <span />
                  <span />
                  <span />
                </div>
              </div>
            )}
          </div>

          {quickReplies.length > 0 && (
            <div className="quick-replies">
              {quickReplies.map((reply) => (
                <button
                  key={reply.value}
                  type="button"
                  disabled={loading}
                  onClick={() => submitMessage(reply.value)}
                >
                  {reply.label}
                </button>
              ))}
            </div>
          )}

          <form className="composer" onSubmit={handleSubmit}>
            <textarea
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={
                missingFields.length > 0
                  ? "Type your answer..."
                  : "Describe the task, or reply to the assistant..."
              }
              rows={2}
            />

            <div className="composer-actions">
              <button
                type="submit"
                className="send-button"
                disabled={loading || !input.trim()}
              >
                Send
              </button>

              {!recording ? (
                <button
                  type="button"
                  className="record-button"
                  onClick={startRecording}
                  disabled={loading}
                  title="Record a voice note"
                >
                  🎤 Record
                </button>
              ) : (
                <button
                  type="button"
                  className="stop-button"
                  onClick={stopRecording}
                >
                  ⏹ Stop
                </button>
              )}

              {audio && !recording && (
                <button
                  type="button"
                  className="send-voice-button"
                  onClick={sendAudio}
                  disabled={loading}
                >
                  Send voice
                </button>
              )}
            </div>
          </form>

          {recording && (
            <p className="recording-status">🔴 Recording in progress...</p>
          )}

          {audioUrl && (
            <audio className="audio-preview" controls src={audioUrl} />
          )}

          {error && <p className="chat-error">{error}</p>}
        </section>

        {savedTask && (
          <aside className="saved-card">
            <h2>Task saved</h2>
            <p className="saved-message">
              {savedMessage || "Your task is now in the database."}
            </p>
            <dl>
              <div>
                <dt>ID</dt>
                <dd>{savedTask.id ?? "-"}</dd>
              </div>
              <div>
                <dt>Title</dt>
                <dd>{savedTask.task_title || "-"}</dd>
              </div>
              <div>
                <dt>Description</dt>
                <dd>{savedTask.task_description || "-"}</dd>
              </div>
              <div>
                <dt>Assigned to</dt>
                <dd>{savedTask.assigned_to || "-"}</dd>
              </div>
              <div>
                <dt>Status</dt>
                <dd>{savedTask.status || "-"}</dd>
              </div>
              <div>
                <dt>Progress</dt>
                <dd>{savedTask.progress ?? "-"}</dd>
              </div>
            </dl>
            <button type="button" className="ghost-button" onClick={startNewTask}>
              Create another task
            </button>
          </aside>
        )}
        </main>
      ) : activeTab === "employees" && canManageEmployeesFlag ? (
        renderEmployeesTab()
      ) : (
        renderTasksTab()
      )}

      {activeTab === "assistant" && status && !savedTask && (
        <footer className="conversation-status">
          Conversation stage: {stage || status}
        </footer>
      )}

      {taskDialog && taskForm && (
        <div
          className="employee-modal-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              closeTaskDialog();
            }
          }}
        >
          <div
            className="employee-modal task-edit-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="task-dialog-title"
          >
            <div className="employee-modal-head">
              <div>
                <p className="eyebrow">Task #{taskDialog.id}</p>
                <h2 id="task-dialog-title">Edit task</h2>
              </div>
              <button
                type="button"
                className="employee-modal-close"
                onClick={closeTaskDialog}
                aria-label="Close dialog"
                disabled={taskSaving}
              >
                ×
              </button>
            </div>

            <form className="employee-form" onSubmit={submitTaskForm}>
              <label>
                <span>Task title</span>
                <input
                  type="text"
                  value={taskForm.task_title}
                  onChange={(event) =>
                    setTaskForm((current) => ({
                      ...current,
                      task_title: event.target.value,
                    }))
                  }
                  required
                  disabled={taskSaving}
                />
              </label>

              <label>
                <span>Description</span>
                <textarea
                  value={taskForm.task_description}
                  onChange={(event) =>
                    setTaskForm((current) => ({
                      ...current,
                      task_description: event.target.value,
                    }))
                  }
                  rows={3}
                  disabled={taskSaving}
                />
              </label>

              <div className="task-edit-grid">
                <label>
                  <span>Assigned date</span>
                  <input
                    type="date"
                    value={taskForm.assigned_date}
                    onChange={(event) =>
                      setTaskForm((current) => ({
                        ...current,
                        assigned_date: event.target.value,
                      }))
                    }
                    disabled={taskSaving}
                  />
                </label>
                <label>
                  <span>Assigned time</span>
                  <input
                    type="time"
                    value={taskForm.assigned_time}
                    onChange={(event) =>
                      setTaskForm((current) => ({
                        ...current,
                        assigned_time: event.target.value,
                      }))
                    }
                    disabled={taskSaving}
                  />
                </label>
                <label>
                  <span>Deadline date</span>
                  <input
                    type="date"
                    value={taskForm.deadline_date}
                    onChange={(event) =>
                      setTaskForm((current) => ({
                        ...current,
                        deadline_date: event.target.value,
                      }))
                    }
                    disabled={taskSaving}
                  />
                </label>
                <label>
                  <span>Deadline time</span>
                  <input
                    type="time"
                    value={taskForm.deadline_time}
                    onChange={(event) =>
                      setTaskForm((current) => ({
                        ...current,
                        deadline_time: event.target.value,
                      }))
                    }
                    disabled={taskSaving}
                  />
                </label>
                <label>
                  <span>Reminder date</span>
                  <input
                    type="date"
                    value={taskForm.reminder_date}
                    onChange={(event) =>
                      setTaskForm((current) => ({
                        ...current,
                        reminder_date: event.target.value,
                      }))
                    }
                    disabled={taskSaving}
                  />
                </label>
                <label>
                  <span>Reminder time</span>
                  <input
                    type="time"
                    value={taskForm.reminder_time}
                    onChange={(event) =>
                      setTaskForm((current) => ({
                        ...current,
                        reminder_time: event.target.value,
                      }))
                    }
                    disabled={taskSaving}
                  />
                </label>
                <label>
                  <span>Status</span>
                  <select
                    value={taskForm.status}
                    onChange={(event) =>
                      setTaskForm((current) => ({
                        ...current,
                        status: event.target.value,
                      }))
                    }
                    disabled={taskSaving}
                  >
                    <option value="PENDING">Pending</option>
                    <option value="IN_PROGRESS">In progress</option>
                    <option value="COMPLETED">Completed</option>
                  </select>
                </label>
                <label>
                  <span>Progress (%)</span>
                  <input
                    type="number"
                    min="0"
                    max="100"
                    step="1"
                    value={taskForm.progress}
                    onChange={(event) =>
                      setTaskForm((current) => ({
                        ...current,
                        progress: event.target.value,
                      }))
                    }
                    disabled={taskSaving}
                  />
                </label>
              </div>

              {taskFormError && <p className="chat-error">{taskFormError}</p>}

              <div className="employee-dialog-actions">
                <button
                  type="button"
                  className="employee-action"
                  onClick={closeTaskDialog}
                  disabled={taskSaving}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="employee-action primary"
                  disabled={taskSaving}
                >
                  {taskSaving ? "Saving…" : "Save changes"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {employeeDialog && (
        <div
          className="employee-modal-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              closeEmployeeDialog();
            }
          }}
        >
          <div
            className="employee-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="employee-dialog-title"
          >
            <div className="employee-modal-head">
              <div>
                <p className="eyebrow">
                  {employeeDialog.mode === "view"
                    ? "Employee details"
                    : "Employee management"}
                </p>
                <h2 id="employee-dialog-title">
                  {employeeDialog.mode === "add"
                    ? "Add employee"
                    : employeeDialog.mode === "edit"
                      ? `Edit ${employeeDialog.employee.name}`
                      : employeeDialog.employee.name}
                </h2>
              </div>
              <button
                type="button"
                className="employee-modal-close"
                onClick={closeEmployeeDialog}
                aria-label="Close dialog"
                disabled={employeeSaving}
              >
                ×
              </button>
            </div>

            {employeeDialog.mode === "view" ? (
              <>
                <dl className="employee-detail-grid">
                  <div>
                    <dt>Email</dt>
                    <dd>{employeeDialog.employee.email}</dd>
                  </div>
                  <div>
                    <dt>Role</dt>
                    <dd>{roleLabel(employeeDialog.employee.role)}</dd>
                  </div>
                  <div>
                    <dt>Status</dt>
                    <dd>
                      {employeeDialog.employee.is_active
                        ? "Active"
                        : "Inactive"}
                    </dd>
                  </div>
                  <div>
                    <dt>Google login</dt>
                    <dd>
                      {employeeDialog.employee.has_logged_in
                        ? "Linked"
                        : "Not signed in yet"}
                    </dd>
                  </div>
                  <div>
                    <dt>Added</dt>
                    <dd>
                      {formatDateTime(employeeDialog.employee.created_at)}
                    </dd>
                  </div>
                  <div>
                    <dt>Last updated</dt>
                    <dd>
                      {formatDateTime(employeeDialog.employee.updated_at)}
                    </dd>
                  </div>
                </dl>

                {employeesError && (
                  <p className="chat-error">{employeesError}</p>
                )}

                {canManageEmployee(employee, employeeDialog.employee) && (
                  <div className="employee-dialog-actions">
                    <button
                      type="button"
                      className="employee-action"
                      onClick={() => openEditEmployee(employeeDialog.employee)}
                      disabled={employeeSaving}
                    >
                      Edit details
                    </button>
                    <button
                      type="button"
                      className="employee-action"
                      onClick={() =>
                        requestEmployeeAction(
                          employeeDialog.employee.is_active
                            ? "deactivate"
                            : "activate",
                          employeeDialog.employee
                        )
                      }
                      disabled={employeeSaving}
                    >
                      {employeeDialog.employee.is_active
                        ? "Deactivate"
                        : "Activate"}
                    </button>
                    <button
                      type="button"
                      className="employee-action danger"
                      onClick={() =>
                        requestEmployeeAction(
                          "revoke-sessions",
                          employeeDialog.employee
                        )
                      }
                      disabled={employeeSaving}
                    >
                      Revoke all sessions
                    </button>
                  </div>
                )}
              </>
            ) : (
              <form className="employee-form" onSubmit={submitEmployeeForm}>
                <label>
                  <span>Full name</span>
                  <input
                    type="text"
                    value={employeeForm.name}
                    onChange={(event) =>
                      setEmployeeForm((current) => ({
                        ...current,
                        name: event.target.value,
                      }))
                    }
                    placeholder="Rahul Sharma"
                    required
                    disabled={employeeSaving}
                  />
                </label>

                <label>
                  <span>Work email</span>
                  <input
                    type="email"
                    value={employeeForm.email}
                    onChange={(event) =>
                      setEmployeeForm((current) => ({
                        ...current,
                        email: event.target.value,
                      }))
                    }
                    placeholder="rahul@example.com"
                    required
                    disabled={
                      employeeSaving || employeeDialog.mode === "edit"
                    }
                  />
                  {employeeDialog.mode === "edit" && (
                    <small>
                      The work email is fixed after provisioning.
                    </small>
                  )}
                </label>

                <label>
                  <span>Role</span>
                  <select
                    value={employeeForm.role}
                    onChange={(event) =>
                      setEmployeeForm((current) => ({
                        ...current,
                        role: event.target.value,
                      }))
                    }
                    disabled={employeeSaving}
                  >
                    {employeeRoleOptions.map((role) => (
                      <option key={role} value={role}>
                        {roleLabel(role)}
                      </option>
                    ))}
                  </select>
                </label>

                {employeesError && (
                  <p className="chat-error">{employeesError}</p>
                )}

                <div className="employee-dialog-actions">
                  <button
                    type="button"
                    className="employee-action"
                    onClick={closeEmployeeDialog}
                    disabled={employeeSaving}
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    className="employee-action primary"
                    disabled={employeeSaving}
                  >
                    {employeeSaving
                      ? "Saving…"
                      : employeeDialog.mode === "add"
                        ? "Add employee"
                        : "Save changes"}
                  </button>
                </div>
              </form>
            )}
          </div>
        </div>
      )}

      {employeeConfirm && employeeConfirmContent && (
        <div className="employee-modal-backdrop employee-confirm-layer">
          <div
            className="employee-modal employee-confirm"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="employee-confirm-title"
          >
            <p className="eyebrow">Confirm action</p>
            <h2 id="employee-confirm-title">
              {employeeConfirmContent.title}
            </h2>
            <p className="employee-confirm-copy">
              {employeeConfirmContent.body}
            </p>

            {employeesError && <p className="chat-error">{employeesError}</p>}

            <div className="employee-dialog-actions">
              <button
                type="button"
                className="employee-action"
                onClick={() => setEmployeeConfirm(null)}
                disabled={employeeSaving}
              >
                Cancel
              </button>
              <button
                type="button"
                className={`employee-action primary ${
                  employeeConfirmContent.danger ? "danger" : ""
                }`}
                onClick={confirmEmployeeAction}
                disabled={employeeSaving}
              >
                {employeeSaving ? "Working…" : employeeConfirmContent.action}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default App;
