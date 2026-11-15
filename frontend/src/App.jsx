import { useCallback, useState } from "react";
import "./App.css";

// The backend runs on port 8000 (see the root README). A different port
// (for example a test server) can be used with VITE_API_BASE_URL, e.g.
// VITE_API_BASE_URL=http://localhost:8001 npm run dev
const API = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";
const CHAT_API = `${API}/tasks/chat`;
const VOICE_TURN_API = `${API}/tasks/voice/turn`;
const TASKS_API = `${API}/tasks`;

const FIELD_META = {
  task_title: { label: "Task title" },
  assigned_to: { label: "Assigned to" },
  assigned_date: { label: "Assigned date" },
  assigned_time: { label: "Assigned time" },
  deadline_date: { label: "Deadline date" },
  deadline_time: { label: "Deadline time" },
  status: { label: "Status" },
  progress: { label: "Progress" },
};

const FIELD_ORDER = Object.keys(FIELD_META);

const WELCOME =
  "Hi! Tell me about the task in your own words. I'll check which details are missing and ask you for them one at a time. You can type or record a voice note.";

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

// The id of the user of this browser. Two users never share a memory.
const storedUserId = () => {
  const key = "task-flow-user-id";

  try {
    const existing = window.localStorage.getItem(key);

    if (existing) return existing;

    const created = `user-${newConversationId()}`;

    window.localStorage.setItem(key, created);

    return created;
  } catch {
    // The storage is not available: the memory is simply not shared.
    return `user-${newConversationId()}`;
  }
};

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

function App() {
  const [messages, setMessages] = useState([
    { id: "welcome", role: "assistant", text: WELCOME },
  ]);
  const [input, setInput] = useState("");

  const [stage, setStage] = useState(null);
  const [draft, setDraft] = useState(null);
  const [status, setStatus] = useState(null);
  const [missingFields, setMissingFields] = useState([]);
  const [savedTask, setSavedTask] = useState(null);
  const [savedMessage, setSavedMessage] = useState("");

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const [conversationId, setConversationId] = useState(newConversationId);
  const [userId] = useState(storedUserId);

  const [activeTab, setActiveTab] = useState("assistant");
  const [tasks, setTasks] = useState([]);
  const [tasksLoading, setTasksLoading] = useState(false);
  const [tasksError, setTasksError] = useState("");
  const [tasksFetchedAt, setTasksFetchedAt] = useState("");

  const [recorder, setRecorder] = useState(null);
  const [recording, setRecording] = useState(false);
  const [audio, setAudio] = useState(null);
  const [audioUrl, setAudioUrl] = useState("");

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

  const loadTasks = useCallback(async () => {
    setTasksLoading(true);
    setTasksError("");

    try {
      const response = await fetch(TASKS_API);
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
  }, []);

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
      },
    ]);
  };

  const applyConversation = (data, shouldSpeak = false) => {
    const reply = data.reply || data.question || data.message || "Done.";

    setStatus(data.status || null);
    setMissingFields(data.missing_fields || []);

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

    addMessage("assistant", reply);

    if (shouldSpeak) {
      speak(reply);
    }
  };

  const submitMessage = async (message) => {
    if (!message || loading) return;

    addMessage("user", message);
    setLoading(true);
    setError("");

    try {
      const response = await fetch(CHAT_API, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          stage,
          draft,
          conversation_id: conversationId,
          user_id: userId,
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || "Chat processing failed.");
      }

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

  const sendTextMessage = () => {
    const message = input.trim();
    if (!message || loading) return;

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
    setMessages([{ id: `welcome-${Date.now()}`, role: "assistant", text: WELCOME }]);
    setInput("");
    setStage(null);
    setDraft(null);
    setStatus(null);
    setMissingFields([]);
    setSavedTask(null);
    setError("");
    setAudio(null);
    setAudioUrl("");
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
    conversationState.user_id = userId;

    if (Object.keys(conversationState).length > 0) {
      formData.append("state", JSON.stringify(conversationState));
    }

    setLoading(true);
    setError("");

    try {
      const response = await fetch(VOICE_TURN_API, {
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

  const collectedCount = FIELD_ORDER.filter(
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
      ];
    }

    if (stage === "reminder") {
      return [
        { label: "30 minutes before deadline", value: "30 minutes before the deadline" },
        { label: "1 hour before deadline", value: "1 hour before deadline" },
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


  const taskStats = {
    total: tasks.length,
    inProgress: tasks.filter((task) => task.status === "IN_PROGRESS").length,
    completed: tasks.filter((task) => task.status === "COMPLETED").length,
    overdue: tasks.filter(isTaskOverdue).length,
  };

  const renderTasksTab = () => (
    <main className="tasks-layout">
      <section className="tasks-panel">
        <div className="tasks-toolbar">
          <div>
            <p className="eyebrow">Workspace</p>
            <h2>All Tasks</h2>
            <p className="tasks-subtitle">
              Every task stored in the task database, with full scheduling
              details.
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
                </tr>
              </thead>
              <tbody>
                {tasks.map((task) => {
                  const progress = Math.min(
                    100,
                    Math.max(0, Number(task.progress) || 0)
                  );

                  return (
                    <tr
                      key={task.id}
                      className={isTaskOverdue(task) ? "overdue-row" : ""}
                    >
                      <td className="id-cell">#{task.id}</td>
                      <td className="title-cell">
                        <strong>{task.task_title || "Untitled task"}</strong>
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

  return (
    <div className={`app ${activeTab === "tasks" ? "tasks-view" : ""}`}>
      <header className="app-header">
        <div>
          <p className="eyebrow">Task Flow Automation</p>
          <h1>Task Assistant</h1>
          <p className="subtitle">
            Create a task through a natural back-and-forth conversation.
          </p>
        </div>

        <button type="button" className="ghost-button" onClick={startNewTask}>
          + New task
        </button>
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
          All Tasks
          {tasks.length > 0 && (
            <span className="tab-badge">{tasks.length}</span>
          )}
        </button>
      </nav>

      {activeTab === "assistant" ? (
        <main className="chat-layout">
        <section className="chat-card">
          {draft && (
            <div className="field-progress">
              <div className="field-progress-head">
                <span>Collected details</span>
                <strong>
                  {collectedCount}/{FIELD_ORDER.length}
                </strong>
              </div>
              <div className="field-chips">
                {FIELD_ORDER.map((field) => {
                  const filled =
                    draft?.[field] !== null && draft?.[field] !== undefined;

                  return (
                    <span
                      key={field}
                      className={`field-chip ${filled ? "filled" : "missing"}`}
                    >
                      {FIELD_META[field].label}
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
                <div className={`bubble ${message.role}`}>{message.text}</div>
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
      ) : (
        renderTasksTab()
      )}

      {activeTab === "assistant" && status && !savedTask && (
        <footer className="conversation-status">
          Conversation stage: {stage || status}
        </footer>
      )}
    </div>
  );
}

export default App;
