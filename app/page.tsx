"use client";

import { useEffect, useRef, useState } from "react";

type OrbState = "idle" | "listening" | "thinking" | "speaking";

const STATUS: Record<OrbState, string> = {
  idle: "tap the light to wake Jonny",
  listening: "listening…",
  thinking: "thinking…",
  speaking: "",
};

// Real-time voice: the mic streams 16kHz PCM straight to the Mac over a
// WebSocket as you talk. The Mac detects when you stop, transcribes the
// audio that's already there, and streams the spoken reply back on the same
// socket — so there's no "upload then wait". Falls back to record-then-post
// if the socket can't open.

export default function Home() {
  const [orbState, setOrbState] = useState<OrbState>("idle");
  const [active, setActive] = useState(false);
  const [lastYou, setLastYou] = useState("");
  const [lastJonny, setLastJonny] = useState("");
  const [error, setError] = useState("");
  const [showTranscript, setShowTranscript] = useState(false);
  const [working, setWorking] = useState(false);

  const activeRef = useRef(false);
  const wsRef = useRef<WebSocket | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const workingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ---------- audio playback queue ----------
  const audioQueueRef = useRef<string[]>([]);
  const currentAudioRef = useRef<HTMLAudioElement | null>(null);

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
      if (activeRef.current) setOrbState("listening");
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
  }

  function clearWorking() {
    if (workingTimerRef.current) clearTimeout(workingTimerRef.current);
    workingTimerRef.current = null;
    setWorking(false);
  }

  // ---------- websocket voice ----------

  async function startWebsocketVoice(): Promise<boolean> {
    let ticket: string, wsUrl: string;
    try {
      const res = await fetch("/api/ws-ticket");
      if (res.status === 401) {
        window.location.href = "/login";
        return true;
      }
      if (!res.ok) return false;
      ({ ticket, wsUrl } = await res.json());
    } catch {
      return false;
    }

    // NATIVE rate — the worklet resamples to 16k itself. Forcing 16000 here
    // breaks on browsers/hardware that don't honor it (audio arrives sped-up
    // and Whisper hears gibberish).
    const ctx = new AudioContext();
    ctxRef.current = ctx;
    console.log(`[jonny] mic pipeline: ${ctx.sampleRate}Hz -> 16k`);
    try {
      await ctx.audioWorklet.addModule("/pcm-worklet.js");
    } catch {
      setError("Couldn't start the audio pipeline (worklet).");
      return false;
    }

    return new Promise<boolean>((resolve) => {
      let settled = false;
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      ws.binaryType = "arraybuffer";

      ws.onopen = () => ws.send(ticket);

      ws.onmessage = (ev) => {
        const event = JSON.parse(ev.data);
        switch (event.type) {
          case "ready": {
            // hook the mic up and start streaming — surface ANY failure
            // (a silently-broken mic chain looks like "it ignores me")
            try {
              const source = ctx.createMediaStreamSource(streamRef.current!);
              const node = new AudioWorkletNode(ctx, "pcm-worklet");
              node.port.onmessage = (m: MessageEvent) => {
                if (ws.readyState === WebSocket.OPEN) ws.send(m.data as ArrayBuffer);
              };
              source.connect(node);
              // worklet needs a destination to pull audio; muted gain avoids echo
              const sink = ctx.createGain();
              sink.gain.value = 0;
              node.connect(sink).connect(ctx.destination);
              setOrbState("listening");
            } catch (e) {
              setError(`Mic pipeline failed: ${e}`);
              if (!settled) {
                settled = true;
                resolve(false);
              }
              break;
            }
            if (!settled) {
              settled = true;
              resolve(true);
            }
            break;
          }
          case "partial": // still talking / paused mid-thought — keep listening
            if (event.text) setLastYou(event.text);
            setOrbState("listening");
            break;
          case "transcript":
            setLastYou(event.text);
            setLastJonny("");
            setError("");
            setOrbState("thinking");
            workingTimerRef.current = setTimeout(() => setWorking(true), 2500);
            break;
          case "interrupt": // you spoke over the reply — cut playback
            stopAllAudio();
            setOrbState("listening");
            break;
          case "delta":
            setLastJonny((t) => t + event.text);
            break;
          case "audio":
            clearWorking();
            enqueueAudio(event.mp3);
            break;
          case "done":
            clearWorking();
            setLastJonny(event.text);
            if (event.research_job_id) pollResearch(event.research_job_id);
            break;
          case "error":
            setError("Something went wrong on the Mac.");
            break;
        }
      };

      ws.onerror = () => {
        if (!settled) {
          settled = true;
          resolve(false);
        }
      };
      ws.onclose = () => {
        if (!settled) {
          settled = true;
          resolve(false);
        } else if (activeRef.current) {
          setError("Connection dropped. Tap the light to reconnect.");
          stopEverything();
        }
      };
    });
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
          const tts = await fetch("/api/tts", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text: data.summary }),
          });
          if (tts.ok) {
            const bytes = new Uint8Array(await tts.arrayBuffer());
            const url = URL.createObjectURL(new Blob([bytes], { type: "audio/mpeg" }));
            audioQueueRef.current.push(url);
            if (!currentAudioRef.current) playNext();
          }
          return;
        }
        if (data.status === "error") return;
      } catch {}
      setTimeout(tick, 5000);
    };
    setTimeout(tick, 5000);
  }

  // ---------- lifecycle ----------

  async function toggleActive() {
    if (active) {
      stopEverything();
      return;
    }
    setError("");
    try {
      streamRef.current = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
      });
    } catch {
      setError("Microphone permission denied.");
      return;
    }
    setActive(true);
    activeRef.current = true;
    setOrbState("thinking");
    const ok = await startWebsocketVoice();
    if (!ok) {
      setError("Couldn't open the live connection — is the Mac on?");
      stopEverything();
    }
  }

  function stopEverything() {
    setActive(false);
    activeRef.current = false;
    clearWorking();
    stopAllAudio();
    try {
      wsRef.current?.close();
    } catch {}
    wsRef.current = null;
    ctxRef.current?.close().catch(() => {});
    ctxRef.current = null;
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    setOrbState("idle");
  }

  useEffect(() => {
    return () => stopEverything();
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
      <p className="status">
        {working ? "working on it…" : active ? STATUS[orbState] : STATUS.idle}
      </p>
      {showTranscript && (
        <div className="transcript">
          {lastYou && <p className="you">you: {lastYou}</p>}
          {lastJonny && <p className="jonny">{lastJonny}</p>}
        </div>
      )}
      {error && <p className="error">{error}</p>}
      {!active && (
        <p className="hint">
          Talk to Jonny naturally — it listens live and answers with the
          Mac&rsquo;s voice. Just start speaking; say &ldquo;stop&rdquo; to cut
          it off.
        </p>
      )}
      <div className="corner-links">
        <button
          className="ghost-btn"
          onClick={() => setShowTranscript((s) => !s)}
          title="Show or hide the conversation text"
        >
          {showTranscript ? "hide text" : "show text"}
        </button>
        <a className="profile-link" href="/about">
          About me
        </a>
      </div>
    </main>
  );
}
