"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

type Result = { question: string; reply: string; error: string } | null;

export function AskJarvis() {
  const router = useRouter();
  const [text, setText] = useState("");
  const [pending, setPending] = useState(false);
  const [result, setResult] = useState<Result>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const question = text.trim();
    if (!question || pending) return;
    setPending(true);
    setResult(null);
    try {
      const res = await fetch("/api/life/ask", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ text: question }),
      });
      if (res.status === 401) {
        window.location.href = "/login";
        return;
      }
      const body = await res.json().catch(() => ({}));
      if (res.ok) {
        setResult({ question, reply: body.text ?? "", error: "" });
        setText("");
      } else {
        setResult({ question, reply: "", error: body.error || `Something went wrong (${res.status}).` });
      }
      router.refresh(); // reminders/tasks Jarvis created show up
    } catch {
      setResult({ question, reply: "", error: "Couldn't reach the website. Check your connection." });
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="ask-wrap">
      <form onSubmit={submit} className="ask">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Ask Jarvis…"
          autoComplete="off"
          disabled={pending}
        />
        <button type="submit" disabled={pending}>
          {pending ? "Thinking…" : "Send"}
        </button>
      </form>
      {pending && <p className="ask-note">Jarvis may bring in an area agent; this can take a minute.</p>}
      {!pending && result?.reply && (
        <p className="ask-reply">
          <span className="muted">you: {result.question}</span>
          {result.reply}
        </p>
      )}
      {!pending && result?.error && <p className="ask-reply error">{result.error}</p>}
    </div>
  );
}
