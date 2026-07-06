"use client";

import { useEffect, useRef, useState } from "react";

type OrbState = "idle" | "listening" | "thinking" | "speaking";
type ChatMessage = { role: "user" | "assistant"; content: string };

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
  const historyRef = useRef<ChatMessage[]>([]);
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

    historyRef.current.push({ role: "user", content: text });
    let reply = "";
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: historyRef.current }),
      });
      if (res.status === 401) {
        window.location.href = "/login";
        return;
      }
      if (!res.ok) throw new Error(`api ${res.status}`);
      reply = (await res.json()).text;
    } catch {
      reply = "Sorry, I hit an error reaching my brain.";
    }

    historyRef.current.push({ role: "assistant", content: reply });
    if (historyRef.current.length > 40) {
      historyRef.current = historyRef.current.slice(-40);
    }
    setLastJonny(reply);
    speak(reply);
  }

  function speak(text: string) {
    const done = () => {
      busyRef.current = false;
      if (activeRef.current) startListening();
      else setOrbState("idle");
    };
    if (typeof speechSynthesis === "undefined") {
      done();
      return;
    }
    speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1.05;
    utterance.onend = done;
    utterance.onerror = done;
    setOrbState("speaking");
    speechSynthesis.speak(utterance);
  }

  function toggleActive() {
    if (active) {
      setActive(false);
      activeRef.current = false;
      busyRef.current = false;
      stopListening();
      if (typeof speechSynthesis !== "undefined") speechSynthesis.cancel();
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
      if (typeof speechSynthesis !== "undefined") speechSynthesis.cancel();
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
    </main>
  );
}
