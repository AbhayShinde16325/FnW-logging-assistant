import { useState, useEffect } from "react";
import "./App.css";

const API = "http://localhost:8000";

const FIELD_META = {
  task_title: { label: "Task title", type: "text" },
  assigned_to: { label: "Assigned to", type: "text" },
  assigned_date: { label: "Assigned date", type: "date" },
  assigned_time: { label: "Assigned time", type: "time" },
  deadline_date: { label: "Deadline date", type: "date" },
  deadline_time: { label: "Deadline time", type: "time" },
  status: { label: "Status", type: "select" },
  progress: { label: "Progress (%)", type: "number" },
};

const FIELD_ORDER = Object.keys(FIELD_META);

function App() {
  const [text, setText] = useState("");
  const [lastText, setLastText] = useState("");
  const [transcript, setTranscript] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  // Staged CREATE flow state
  const [stage, setStage] = useState(null);
  const [draft, setDraft] = useState(null);
  const [question, setQuestion] = useState("");
  const [missingFields, setMissingFields] = useState([]);
  const [answers, setAnswers] = useState({});
  const [editing, setEditing] = useState(false);
  const [editValues, setEditValues] = useState({});
  const [reminderText, setReminderText] = useState("");
  const [reminder, setReminder] = useState(null);

  const [recorder, setRecorder] = useState(null);
  const [recording, setRecording] = useState(false);
  const [audio, setAudio] = useState(null);
  const [audioUrl, setAudioUrl] = useState("");

  useEffect(() => {
    if (!audio) {
      setAudioUrl("");
      return;
    }

    const url = URL.createObjectURL(audio);
    setAudioUrl(url);

    return () => URL.revokeObjectURL(url);
  }, [audio]);

  const applyResult = (data) => {
    setResult(data);
    setStage(data.stage || null);
    setDraft(data.draft || null);
    setQuestion(data.question || data.message || "");
    setMissingFields(data.missing_fields || []);
    setAnswers({});
    setEditing(false);
    setReminder(data.reminder || null);
    setReminderText("");

    if (data.status === "success") {
      setStage(null);
      setDraft(null);
    }
  };

  const postStage = async (payload) => {
    setLoading(true);
    setError("");

    try {
      const response = await fetch(`${API}/tasks/text`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || "Task processing failed");
      }

      applyResult(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  // Start recording
  const startRecording = async () => {
    try {
      setError("");
      setAudio(null);
      setTranscript("");
      setResult(null);

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

  // Send voice to FastAPI
  const sendAudio = async () => {
    if (!audio) return;

    const extension = audio.type.includes("ogg")
      ? "ogg"
      : audio.type.includes("mp4")
        ? "mp4"
        : "webm";

    const formData = new FormData();

    formData.append("file", audio, `recording.${extension}`);

    setLoading(true);
    setError("");
    setResult(null);

    try {
      const response = await fetch(`${API}/tasks/voice`, {
        method: "POST",
        body: formData,
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || "Transcription failed");
      }

      setTranscript(data.transcription);
      setLastText(data.transcription);
      applyResult(data.result);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  // Send typed task to FastAPI (starts the staged flow)
  const sendText = async () => {
    if (!text.trim()) return;

    setTranscript("");
    setLastText(text);

    await postStage({ text: text, stage: "start" });
  };

  const submitClarify = () =>
    postStage({ text: lastText, stage: "clarify", draft, answers });

  const confirmFields = () =>
    postStage({ text: lastText, stage: "confirm_fields", draft, confirm: true });

  const startEditing = () => {
    const values = {};
    FIELD_ORDER.forEach((field) => {
      values[field] = draft?.[field] ?? "";
    });
    setEditValues(values);
    setEditing(true);
  };

  const saveEdits = () =>
    postStage({
      text: lastText,
      stage: "confirm_fields",
      draft,
      answers: editValues,
    });

  const submitReminder = () =>
    postStage({
      text: lastText,
      stage: "reminder",
      draft,
      reminder_text: reminderText,
    });

  const confirmReminder = () =>
    postStage({ text: lastText, stage: "confirm_reminder", draft, confirm: true });

  const renderFieldInput = (field, value, onChange) => {
    const meta = FIELD_META[field];

    if (meta.type === "select") {
      return (
        <select value={value || ""} onChange={(e) => onChange(e.target.value)}>
          <option value="">-- select --</option>
          <option value="PENDING">PENDING</option>
          <option value="IN_PROGRESS">IN_PROGRESS</option>
          <option value="COMPLETED">COMPLETED</option>
        </select>
      );
    }

    return (
      <input
        type={meta.type}
        value={value || ""}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  };

  const renderStage = () => {
    if (!result) return null;

    if (result.status === "error") {
      return (
        <section className="card error">
          <p>{result.message}</p>
        </section>
      );
    }

    if (result.status === "success") {
      return (
        <section className="card result">
          <h2>Task Saved</h2>
          <p>{result.message}</p>
          {result.task && <pre>{JSON.stringify(result.task, null, 2)}</pre>}
        </section>
      );
    }

    if (
      result.status === "clarification_required" &&
      stage === "fields" &&
      !editing
    ) {
      return (
        <section className="card">
          <h2>Fill in the details</h2>
          <p>{question}</p>

          {missingFields.length > 0 && (
            <div>
              {missingFields.map((field) => (
                <div key={field} style={{ marginBottom: "8px" }}>
                  <label>{FIELD_META[field].label}: </label>
                  {renderFieldInput(field, answers[field], (value) =>
                    setAnswers({ ...answers, [field]: value })
                  )}
                </div>
              ))}
            </div>
          )}

          <button onClick={submitClarify} disabled={loading}>
            Submit details
          </button>
        </section>
      );
    }

    if (result.status === "confirmation_required" && stage === "confirm_fields") {
      return (
        <section className="card result">
          <h2>Confirm task details</h2>
          <p>{question}</p>

          {!editing && (
            <table>
              <tbody>
                {FIELD_ORDER.map((field) => (
                  <tr key={field}>
                    <th>{FIELD_META[field].label}</th>
                    <td>{draft?.[field] ?? "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {editing && (
            <div>
              {FIELD_ORDER.map((field) => (
                <div key={field} style={{ marginBottom: "8px" }}>
                  <label>{FIELD_META[field].label}: </label>
                  {renderFieldInput(field, editValues[field], (value) =>
                    setEditValues({ ...editValues, [field]: value })
                  )}
                </div>
              ))}
            </div>
          )}

          {!editing && (
            <div>
              <button onClick={confirmFields} disabled={loading}>
                ✅ Confirm details
              </button>{" "}
              <button onClick={startEditing} disabled={loading}>
                ✏️ Edit
              </button>
            </div>
          )}

          {editing && (
            <div>
              <button onClick={saveEdits} disabled={loading}>
                Save edits
              </button>{" "}
              <button onClick={() => setEditing(false)} disabled={loading}>
                Cancel
              </button>
            </div>
          )}
        </section>
      );
    }

    if (result.status === "reminder_required" && stage === "reminder") {
      return (
        <section className="card">
          <h2>Reminder</h2>
          <p>{question}</p>

          <input
            type="text"
            value={reminderText}
            placeholder="e.g. 30 minutes before the deadline, or 15 November 2026 at 4 PM"
            onChange={(e) => setReminderText(e.target.value)}
            style={{ width: "80%" }}
          />{" "}
          <button onClick={submitReminder} disabled={loading || !reminderText.trim()}>
            Set reminder
          </button>
        </section>
      );
    }

    if (
      result.status === "reminder_confirmation_required" &&
      stage === "confirm_reminder"
    ) {
      return (
        <section className="card result">
          <h2>Confirm reminder</h2>
          <p>{question}</p>

          {reminder && (
            <p>
              <strong>Reminder:</strong> {reminder.display}
            </p>
          )}

          <div>
            <button onClick={confirmReminder} disabled={loading}>
              ✅ Save task with this reminder
            </button>{" "}
            <button onClick={() => setStage("reminder")} disabled={loading}>
              🔄 Change reminder
            </button>
          </div>
        </section>
      );
    }

    return (
      <section className="card result">
        <h2>Task Processing Result</h2>
        <p>
          <strong>Status:</strong> {result.status}
        </p>
        <p>{result.message}</p>
        {result.task && <pre>{JSON.stringify(result.task, null, 2)}</pre>}
      </section>
    );
  };

  return (
    <div className="app">
      <header>
        <h1>Task Flow Automation</h1>
        <p>Create and manage tasks using AI voice or text.</p>
      </header>

      <main>
        {/* Text input */}
        <section className="card">
          <h2>Create Task Using Text</h2>

          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Example: Create a task to complete the dashboard by Friday..."
          />

          <button onClick={sendText} disabled={loading || !text.trim()}>
            Submit Task
          </button>
        </section>

        {/* Voice input */}
        <section className="card">
          <h2>Create Task Using Voice</h2>

          <div className="voice-controls">
            {!recording ? (
              <button
                className="record"
                onClick={startRecording}
                disabled={loading}
              >
                🎤 Record Audio
              </button>
            ) : (
              <button className="stop" onClick={stopRecording}>
                ⏹ Stop Recording
              </button>
            )}

            <button
              onClick={sendAudio}
              disabled={!audio || recording || loading}
            >
              {loading ? "Processing..." : "📤 Send Audio"}
            </button>
          </div>

          {recording && (
            <p className="recording-status">🔴 Recording in progress...</p>
          )}

          {audioUrl && <audio controls src={audioUrl} />}
        </section>

        {/* Transcription */}
        {transcript && (
          <section className="card">
            <h2>Voice Transcription</h2>
            <p>{transcript}</p>
          </section>
        )}

        {/* Staged flow */}
        {renderStage()}

        {/* Error */}
        {error && <section className="error">{error}</section>}

        {loading && <p className="loading">AI is processing your request...</p>}
      </main>
    </div>
  );
}

export default App;
