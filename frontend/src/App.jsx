import { useState, useEffect } from "react";
import "./App.css";

const API = "http://localhost:8000";

function App() {
  const [text, setText] = useState("");
  const [recording, setRecording] = useState(false);
  const [audio, setAudio] = useState(null);
  const [audioUrl, setAudioUrl] = useState("");
  const [loading, setLoading] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");

  const [recorder, setRecorder] = useState(null);

  useEffect(() => {
    if (!audio) {
      setAudioUrl("");
      return;
    }

    const url = URL.createObjectURL(audio);
    setAudioUrl(url);

    return () => URL.revokeObjectURL(url);
  }, [audio]);

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

  // Stop recording
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
      setResult(data.result);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  // Send typed task to FastAPI
  const sendText = async () => {
    if (!text.trim()) return;

    setLoading(true);
    setError("");
    setResult(null);
    setTranscript("");

    try {
      const response = await fetch(`${API}/tasks/text`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          text: text,
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || "Task processing failed");
      }

      setResult(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
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

        {/* Error */}
        {error && <section className="error">{error}</section>}

        {/* Transcription */}
        {transcript && (
          <section className="card">
            <h2>Voice Transcription</h2>
            <p>{transcript}</p>
          </section>
        )}

        {/* Task result */}
        {result && (
          <section className="card result">
            <h2>Task Processing Result</h2>

            <p>
              <strong>Status:</strong> {result.status}
            </p>

            <p>
              <strong>Operation:</strong> {result.operation}
            </p>

            <p>{result.message}</p>

            {result.task && <pre>{JSON.stringify(result.task, null, 2)}</pre>}

            {result.status === "confirmation_required" && (
              <p>Please select the task and confirm deletion.</p>
            )}

            {result.status === "task_selection_required" && (
              <pre>{JSON.stringify(result.tasks, null, 2)}</pre>
            )}
          </section>
        )}

        {loading && <p className="loading">AI is processing your request...</p>}
      </main>
    </div>
  );
}

export default App;
