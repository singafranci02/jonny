"use client";

import { useEffect, useRef, useState } from "react";

type OrbState = "idle" | "listening" | "thinking" | "speaking";

const STATUS: Record<OrbState, string> = {
  idle: "tap the light to wake Jonny",
  listening: "listening…",
  thinking: "thinking…",
  speaking: "",
};

export default function Home() {
  const [orbState, setOrbState] = useState<OrbState>("idle");
  const [active, setActive] = useState(false);
  const [lastYou, setLastYou] = useState("");
  const [lastJonny, setLastJonny] = useState("");
  const [error, setError] = useState("");

  const recognitionRef = useRef<any>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const activeRef = useRef(false);
  const busyRef = useRef(false); // true while thinking or speaking

  activeRef.current = active;

  function getRecognitionCtor(): any {
    if (typeof window === "undefined") return null;
    return (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
  }

  function startListening() {
    if (!activeRef.current || busyRef.current) return;
    const Ctor = getRecognitionCtor();
    if (!Ctor) {
      setError("This browser has no speech recognition. Use Chrome, Edge, or Safari.");
      return;
    }
    // one recognizer per utterance keeps state machines simple
    const rec = new Ctor();
    recognitionRef.current = rec;
    rec.lang = "en-US";
    rec.continuous = false;
    rec.interimResults = false;

    rec.onresult = (event: any) => {
      const text = (event.results?.[0]?.[0]?.transcript || "").trim();
      if (text) void handleUtterance(text);
    };
    rec.onerror = (event: any) => {
      if (event.error === "not-allowed") {
        setError("Microphone permission denied.");
        setActive(false);
        setOrbState("idle");
      }
      // "no-speech" and "aborted" are routine; onend restarts us
    };
    rec.onend = () => {
      // silence timeout or utterance captured; loop while active
      if (activeRef.current && !busyRef.current) startListening();
    };

    try {
      rec.start();
      setOrbState("listening");
    } catch {
      /* start() throws if called while already running; ignore */
    }
  }

  function stopListening() {
    try {
      recognitionRef.current?.abort();
    } catch {}
    recognitionRef.current = null;
  }

  async function handleUtterance(text: string) {
    busyRef.current = true;
    stopListening();
    setLastYou(text);
    setError("");
    setOrbState("thinking");

    // The Mac brain holds the real conversation history — send only the message.
    let reply = "";
    let researchJobId: string | null = null;
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
      if (res.status === 401) {
        window.location.href = "/login";
        return;
      }
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        reply = body.error || "Sorry, I couldn't reach my brain.";
      } else {
        const data = await res.json();
        reply = data.text;
        researchJobId = data.researchJobId;
      }
    } catch {
      reply = "Sorry, I can't reach the Mac right now.";
    }

    setLastJonny(reply);
    speak(reply, () => {
      if (researchJobId) pollResearch(researchJobId);
    });
  }

  function pollResearch(jobId: string) {
    const started = Date.now();
    const tick = async () => {
      if (Date.now() - started > 10 * 60 * 1000) return; // give up after 10 min
      try {
        const res = await fetch(`/api/research?job=${encodeURIComponent(jobId)}`);
        const data = await res.json();
        if (data.status === "done") {
          setLastJonny(data.summary);
          speak(data.summary);
          return;
        }
        if (data.status === "error") return;
      } catch {
        /* keep polling */
      }
      setTimeout(tick, 5000);
    };
    setTimeout(tick, 5000);
  }

  // Play the Mac's Kokoro voice; fall back to the browser voice if unreachable.
  async function speak(text: string, after?: () => void) {
    const done = () => {
      busyRef.current = false;
      if (after) after();
      else if (activeRef.current) startListening();
      else setOrbState("idle");
    };
    setOrbState("speaking");
    try {
      const res = await fetch("/api/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (res.ok) {
        const blob = await res.blob();
        const audio = new Audio(URL.createObjectURL(blob));
        audioRef.current = audio;
        audio.onended = done;
        audio.onerror = () => browserSpeak(text, done);
        await audio.play();
        return;
      }
    } catch {
      /* fall through to browser voice */
    }
    browserSpeak(text, done);
  }

  function browserSpeak(text: string, done: () => void) {
    if (typeof speechSynthesis === "undefined") {
      done();
      return;
    }
    speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1.05;
    utterance.onend = done;
    utterance.onerror = done;
    speechSynthesis.speak(utterance);
  }

  function stopSpeaking() {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
    }
    if (typeof speechSynthesis !== "undefined") speechSynthesis.cancel();
  }

  function toggleActive() {
    if (active) {
      setActive(false);
      activeRef.current = false;
      busyRef.current = false;
      stopListening();
      stopSpeaking();
      setOrbState("idle");
    } else {
      setError("");
      setActive(true);
      activeRef.current = true;
      startListening();
    }
  }

  useEffect(() => {
    return () => {
      stopListening();
      stopSpeaking();
    };
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
          Jonny listens continuously while awake and answers out loud.
        </p>
      )}
      <a className="profile-link" href="/about">
        About me
      </a>
    </main>
  );
}
