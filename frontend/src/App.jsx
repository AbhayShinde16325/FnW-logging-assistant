import { useState } from "react";
import "./App.css";

const API = "http://localhost:8000";
const CHAT_API = `${API}/tasks/chat`;
const VOICE_TURN_API = `${API}/tasks/voice/turn`;

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

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

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

    if (data.status === "success") {
      setStage(null);
      setDraft(null);
      setSavedTask(data.task || null);
    } else {
      if (data.stage) setStage(data.stage);
      if (data.draft) setDraft(data.draft);
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
        body: JSON.stringify({ message, stage, draft }),
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
        { label: "1 hour before deadline", value: "1 hour before the deadline" },
      ];
    }

    return [];
  })();

  return (
    <div className="app">
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
            <p className="saved-message">Your task is now in the database.</p>
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

      {status && !savedTask && (
        <footer className="conversation-status">
          Conversation stage: {stage || status}
        </footer>
      )}
    </div>
  );
}

export default App;
