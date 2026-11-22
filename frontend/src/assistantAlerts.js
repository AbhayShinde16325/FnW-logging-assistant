const STORAGE_KEY = "taskflow-assistant-alerts-v1";

export const PRIORITIES = {
  LOW: "LOW",
  NORMAL: "NORMAL",
  HIGH: "HIGH",
  CRITICAL: "CRITICAL",
};

const RANK = { LOW: 0, NORMAL: 1, HIGH: 2, CRITICAL: 3 };

export function normalizePriority(value) {
  const key = String(value || "NORMAL").toUpperCase();
  return RANK[key] === undefined ? "NORMAL" : key;
}

export function loadAlertPreferences() {
  const defaults = {
    attentionEnabled: false,
    voiceEnabled: true,
    soundEnabled: true,
    volume: 0.7,
    rate: 1,
    pitch: 1,
  };

  if (typeof window === "undefined" || !window.localStorage) return defaults;

  try {
    const raw = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "{}");
    return {
      ...defaults,
      ...raw,
      volume: clamp(Number(raw.volume ?? defaults.volume), 0, 1),
      rate: clamp(Number(raw.rate ?? defaults.rate), 0.6, 1.4),
      pitch: clamp(Number(raw.pitch ?? defaults.pitch), 0.7, 1.4),
    };
  } catch {
    return defaults;
  }
}

export function saveAlertPreferences(prefs) {
  if (typeof window === "undefined" || !window.localStorage) return;
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
}

function clamp(value, min, max) {
  if (Number.isNaN(value)) return min;
  return Math.min(max, Math.max(min, value));
}

export function isSpokenReminderType(notification) {
  const type = String(
    notification?.notification_type || notification?.payload?.notification_type || ""
  ).toUpperCase();
  return [
    "TASK_REMINDER",
    "DEADLINE_APPROACHING",
    "TASK_OVERDUE",
    "TASK_ASSIGNED",
  ].includes(type);
}

export function shouldAnnounceNotification(notification, prefs) {
  const priority = normalizePriority(notification?.priority || notification?.payload?.priority);

  if (!prefs?.attentionEnabled) return { speak: false, sound: false, priority };

  if (priority === "LOW") {
    return { speak: false, sound: false, priority };
  }

  const spokenReminder = isSpokenReminderType(notification);

  if (priority === "NORMAL") {
    return {
      speak: Boolean(prefs.voiceEnabled) && spokenReminder,
      sound: Boolean(prefs.soundEnabled),
      priority,
    };
  }

  return {
    speak: Boolean(prefs.voiceEnabled),
    sound: Boolean(prefs.soundEnabled),
    priority,
  };
}

export function announcementText(notification) {
  const direct = notification?.voice_text || notification?.payload?.voice_text;
  if (direct) return String(direct);

  const title = String(notification?.title || "Task reminder").trim();
  const message = String(notification?.message || "").trim();
  return `${title}. ${message}`.trim();
}

export function selectVoice(voices = []) {
  const preferred = [
    /en[-_]?IN/i,
    /en[-_]?GB/i,
    /en[-_]?US/i,
    /^en/i,
  ];

  for (const pattern of preferred) {
    const match = voices.find((voice) => pattern.test(voice.lang || ""));
    if (match) return match;
  }

  return voices[0] || null;
}

export function createAlertController({
  getPreferences,
  onSpoken,
  onSoundError,
} = {}) {
  const announced = new Set();
  let speaking = false;
  let queue = [];
  let audioContext = null;
  let lastSoundAt = 0;

  function prefs() {
    return getPreferences ? getPreferences() : loadAlertPreferences();
  }

  function markAnnounced(notification) {
    if (notification?.id != null) announced.add(notification.id);
  }

  function wasAnnounced(notification) {
    return notification?.id != null && announced.has(notification.id);
  }

  async function ensureAudioReady() {
    const currentPrefs = prefs();
    if (!currentPrefs.attentionEnabled || !currentPrefs.soundEnabled) return false;

    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!AudioContext) return false;

    if (!audioContext) {
      audioContext = new AudioContext();
    }

    if (audioContext.state === "suspended") {
      try {
        await audioContext.resume();
      } catch {
        onSoundError?.("Audio playback was blocked by the browser.");
        return false;
      }
    }

    return audioContext.state === "running";
  }

  async function playTone(priority = "NORMAL") {
    const currentPrefs = prefs();
    const ready = await ensureAudioReady();
    if (!ready) return false;

    const nowMs = Date.now();
    if (nowMs - lastSoundAt < 400) return false;
    lastSoundAt = nowMs;

    const context = audioContext;
    const base = priority === "CRITICAL" ? 880 : priority === "HIGH" ? 740 : 660;
    const pattern =
      priority === "CRITICAL"
        ? [1, 1, 1, 0, 1, 1]
        : priority === "HIGH"
          ? [1, 1, 0, 1]
          : [1, 1];
    const volume = clamp(currentPrefs.volume, 0, 1) * 0.22;

    try {
      let t = context.currentTime;
      for (const pulse of pattern) {
        if (pulse) {
          const osc = context.createOscillator();
          const gain = context.createGain();
          osc.type = priority === "CRITICAL" ? "square" : "sine";
          osc.frequency.setValueAtTime(base, t);
          osc.frequency.exponentialRampToValueAtTime(base * 1.18, t + 0.08);
          osc.frequency.exponentialRampToValueAtTime(base * 0.9, t + 0.18);
          gain.gain.setValueAtTime(0.0001, t);
          gain.gain.exponentialRampToValueAtTime(Math.max(volume, 0.0001), t + 0.015);
          gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.18);
          osc.connect(gain);
          gain.connect(context.destination);
          osc.start(t);
          osc.stop(t + 0.2);
        }
        t += 0.22;
      }
      return true;
    } catch (error) {
      onSoundError?.(String(error?.message || error));
      return false;
    }
  }

  function speakNext() {
    const currentPrefs = prefs();
    if (!currentPrefs.attentionEnabled || !currentPrefs.voiceEnabled) {
      queue = [];
      speaking = false;
      return;
    }

    if (speaking || queue.length === 0) return;
    if (typeof window === "undefined" || !window.speechSynthesis) return;

    const item = queue.shift();
    speaking = true;

    try {
      const utterance = new SpeechSynthesisUtterance(item.text);
      const voices = window.speechSynthesis.getVoices();
      const voice = selectVoice(voices);
      if (voice) utterance.voice = voice;
      utterance.rate = clamp(currentPrefs.rate, 0.6, 1.4);
      utterance.pitch = clamp(currentPrefs.pitch, 0.7, 1.4);
      utterance.volume = clamp(currentPrefs.volume, 0, 1);
      utterance.lang = voice?.lang || "en-IN";

      const done = () => {
        speaking = false;
        onSpoken?.(item.notification);
        speakNext();
      };

      utterance.onend = done;
      utterance.onerror = done;
      window.speechSynthesis.speak(utterance);
    } catch {
      speaking = false;
      speakNext();
    }
  }

  function enqueueSpeech(notification, priority) {
    const currentPrefs = prefs();
    if (!currentPrefs.attentionEnabled || !currentPrefs.voiceEnabled) return;
    if (typeof window === "undefined" || !window.speechSynthesis) return;

    const text = announcementText(notification);
    if (!text) return;

    queue.push({ notification, priority, text, id: notification?.id });
    queue.sort((a, b) => RANK[b.priority] - RANK[a.priority]);

    if (window.speechSynthesis.getVoices().length === 0) {
      window.speechSynthesis.addEventListener?.("voiceschanged", speakNext, { once: true });
    }

    // Some browsers pause speech synthesis unexpectedly.
    try {
      window.speechSynthesis.resume?.();
    } catch {
      // ignore
    }

    speakNext();
  }

  async function handleNotification(notification, { allowSpeech = true } = {}) {
    if (!notification || notification.is_read || wasAnnounced(notification)) return { handled: false };

    const currentPrefs = prefs();
    const decision = shouldAnnounceNotification(notification, currentPrefs);
    if (!decision.speak && !decision.sound) {
      markAnnounced(notification);
      return { handled: false, priority: decision.priority };
    }

    markAnnounced(notification);

    let soundPlayed = false;
    if (decision.sound) {
      soundPlayed = await playTone(decision.priority);
    }

    if (allowSpeech && decision.speak) {
      // Let the alarm tone be heard before the spoken reminder.
      const delay = soundPlayed ? 450 : 0;
      window.setTimeout(() => {
        enqueueSpeech(notification, decision.priority);
      }, delay);
    }

    return { handled: true, soundPlayed, spokenQueued: allowSpeech && decision.speak, priority: decision.priority };
  }

  function test({ voice = true, sound = true } = {}) {
    const sample = {
      id: `test-${Date.now()}`,
      priority: "HIGH",
      title: "Test notification",
      message: "This is a test of Task Flow attention alerts.",
      voice_text:
        "Attention. This is a test of Task Flow voice and alarm alerts.",
    };
    if (sound) playTone("HIGH");
    if (voice) enqueueSpeech(sample, "HIGH");
  }

  function stopSpeech() {
    if (typeof window !== "undefined" && window.speechSynthesis) {
      window.speechSynthesis.cancel();
    }
    queue = [];
    speaking = false;
  }

  function dispose() {
    stopSpeech();
    if (audioContext) {
      audioContext.close().catch(() => {});
      audioContext = null;
    }
  }

  return {
    handleNotification,
    ensureAudioReady,
    playTone,
    test,
    stopSpeech,
    dispose,
    wasAnnounced,
  };
}

export function diffNewNotifications(current = [], previousIds = new Set()) {
  return current.filter((item) => item?.id != null && !previousIds.has(item.id));
}

export function notificationIds(items = []) {
  return new Set(items.map((item) => item.id).filter((id) => id != null));
}
