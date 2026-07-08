"use client";

import { useEffect, useRef, useState } from "react";

type OrbState = "idle" | "listening" | "thinking" | "speaking";

const STATUS: Record<OrbState, string> = {
  idle: "tap the light to wake Jonny",
  listening: "listening…",
  thinking: "thinking…",
  speaking: "",
};

// Voice pipeline: mic -> (silence detection) -> Mac Whisper -> streamed
// reply, spoken sentence-by-sentence with the Mac's own voice while the
// model is still generating.

export default function Home() {
  const [orbState, setOrbState] = useState<OrbState>("idle");
  const [active, setActive] = useState(false);
  const [lastYou, setLastYou] = useState("");
  const [lastJonny, setLastJonny] = useState("");
  const [error, setError] = useState("");

  const activeRef = useRef(false);
  const streamRef = useRef<MediaStream | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const audioQueueRef = useRef<string[]>([]); // object URLs waiting to play
  const currentAudioRef = useRef<HTMLAudioElement | null>(null);
  const playbackDoneRef = useRef<(() => void) | null>(null);

  // ---------- audio playback queue ----------

  function enqueueAudio(mp3b64: string) {
    const bytes = Uint8Array.from(atob(mp3b64), (c) => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: "audio/mpeg" }));
    audioQueueRef.current.push(url);
    if (!currentAudioRef.current) playNext();
  }

  function playNext() {
    const url = audioQueueRef.current.shift();
    if (!url) {
      currentAudioRef.current = null;
      playbackDoneRef.current?.();
      return;
    }
    setOrbState("speaking");
    const audio = new Audio(url);
    currentAudioRef.current = audio;
    audio.onended = audio.onerror = () => {
      URL.revokeObjectURL(url);
      playNext();
    };
    audio.play().catch(() => playNext());
  }

  function stopAllAudio() {
    audioQueueRef.current.forEach((u) => URL.revokeObjectURL(u));
    audioQueueRef.current = [];
    currentAudioRef.current?.pause();
    currentAudioRef.current = null;
    playbackDoneRef.current = null;
  }

  function waitForPlaybackDrained(): Promise<void> {
    if (!currentAudioRef.current && audioQueueRef.current.length === 0) {
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      playbackDoneRef.current = () => {
        playbackDoneRef.current = null;
        resolve();
      };
    });
  }

  // ---------- record one utterance (RMS-based end of speech) ----------

  function recordUtterance(stream: MediaStream): Promise<Blob | null> {
    return new Promise((resolve) => {
      const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"].find(
        (m) => typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported(m),
      );
      if (!mime) return resolve(null);

      const ctx = new AudioContext();
      const source = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 1024;
      source.connect(analyser);
      const buf = new Float32Array(analyser.fftSize);

      const recorder = new MediaRecorder(stream, { mimeType: mime });
      const chunks: BlobPart[] = [];
      recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);

      let speechStarted = false;
      let silentMs = 0;
      let elapsedMs = 0;
      const TICK = 60;

      const timer = setInterval(() => {
        elapsedMs += TICK;
        analyser.getFloatTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
        const rms = Math.sqrt(sum / buf.length);

        if (!speechStarted && rms > 0.02) speechStarted = true;
        if (speechStarted) {
          silentMs = rms < 0.012 ? silentMs + TICK : 0;
        }

        const done =
          (speechStarted && silentMs >= 1100) || // finished talking
          (!speechStarted && elapsedMs >= 9000) || // nobody spoke
          elapsedMs >= 30000 || // hard cap
          !activeRef.current;
        if (done) {
          clearInterval(timer);
          recorder.onstop = () => {
            ctx.close();
            resolve(speechStarted ? new Blob(chunks, { type: mime }) : null);
          };
          try {
            recorder.stop();
          } catch {
            ctx.close();
            resolve(null);
          }
        }
      }, TICK);

      recorder.start(250);
    });
  }

  // ---------- one full voice turn ----------

  async function voiceLoop() {
    while (activeRef.current) {
      setOrbState("listening");
      const stream = streamRef.current;
      if (!stream) break;
      const blob = await recordUtterance(stream);
      if (!activeRef.current) break;
      if (!blob) continue;

      // transcribe on the Mac (Whisper)
      setOrbState("thinking");
      let text = "";
      try {
        const res = await fetch("/api/stt", { method: "POST", body: blob });
        if (res.status === 401) return void (window.location.href = "/login");
        text = (await res.json()).text ?? "";
      } catch {
        /* mic noise or network — just listen again */
      }
      if (!text.trim()) continue;
      setLastYou(text);
      setLastJonny("");
      setError("");

      // stream the reply; audio starts on the first sentence
      let researchJobId: string | null = null;
      try {
        abortRef.current = new AbortController();
        const res = await fetch("/api/chat-stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text }),
          signal: abortRef.current.signal,
        });
        if (res.status === 401) return void (window.location.href = "/login");
        if (!res.ok || !res.body) throw new Error("brain offline");

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let carry = "";
        let shownText = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          carry += decoder.decode(value, { stream: true });
          const frames = carry.split("\n\n");
          carry = frames.pop() ?? "";
          for (const frame of frames) {
            const line = frame.trim();
            if (!line.startsWith("data: ")) continue;
            const event = JSON.parse(line.slice(6));
            if (event.type === "delta") {
              shownText += event.text;
              setLastJonny(shownText);
            } else if (event.type === "audio") {
              enqueueAudio(event.mp3);
            } else if (event.type === "done") {
              setLastJonny(event.text);
              researchJobId = event.research_job_id;
            } else if (event.type === "error") {
              setError("Something went wrong on the Mac.");
            }
          }
        }
      } catch {
        setError("Can't reach the Mac — is it on and the tunnel running?");
        setLastJonny("");
        await new Promise((r) => setTimeout(r, 1500));
        continue;
      }

      await waitForPlaybackDrained();
      if (researchJobId) pollResearch(researchJobId);
    }
    setOrbState("idle");
  }

  function pollResearch(jobId: string) {
    const started = Date.now();
    const tick = async () => {
      if (Date.now() - started > 10 * 60 * 1000) return;
      try {
        const res = await fetch(`/api/research?job=${encodeURIComponent(jobId)}`);
        const data = await res.json();
        if (data.status === "done") {
          setLastJonny(data.summary);
          try {
            const tts = await fetch("/api/tts", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ text: data.summary }),
            });
            if (tts.ok) {
              const url = URL.createObjectURL(await tts.blob());
              audioQueueRef.current.push(url);
              if (!currentAudioRef.current) playNext();
            }
          } catch {}
          return;
        }
        if (data.status === "error") return;
      } catch {}
      setTimeout(tick, 5000);
    };
    setTimeout(tick, 5000);
  }

  // ---------- wake / sleep ----------

  async function toggleActive() {
    if (active) {
      setActive(false);
      activeRef.current = false;
      abortRef.current?.abort();
      stopAllAudio();
      streamRef.current?.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
      setOrbState("idle");
      return;
    }
    setError("");
    try {
      streamRef.current = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
    } catch {
      setError("Microphone permission denied.");
      return;
    }
    setActive(true);
    activeRef.current = true;
    void voiceLoop();
  }

  useEffect(() => {
    return () => {
      activeRef.current = false;
      abortRef.current?.abort();
      stopAllAudio();
      streamRef.current?.getTracks().forEach((t) => t.stop());
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <main className="stage">
      <div
        className={`orb orb-${orbState}`}
        onClick={toggleActive}
        role="button"
        aria-label={active ? "Put Jonny to sleep" : "Wake Jonny"}
        title={active ? "Click to stop" : "Click to talk"}
      />
      <h1>Jonny</h1>
      <p className="status">{active ? STATUS[orbState] : STATUS.idle}</p>
      <div className="transcript">
        {lastYou && <p className="you">you: {lastYou}</p>}
        {lastJonny && <p className="jonny">{lastJonny}</p>}
      </div>
      {error && <p className="error">{error}</p>}
      {!active && (
        <p className="hint">
          Jonny hears with the Mac&rsquo;s ears and speaks with its voice —
          click the light and talk.
        </p>
      )}
      <a className="profile-link" href="/about">
        About me
      </a>
    </main>
  );
}
