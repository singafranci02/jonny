"use client";

import { useEffect, useState } from "react";

// The "About Me" profile — hand-edited steering the assistant always knows.
// Saved on the Mac; both the website and the Mac voice assistant read it.

export default function AboutPage() {
  const [content, setContent] = useState("");
  const [status, setStatus] = useState<"loading" | "ready" | "saving" | "saved" | "error">(
    "loading",
  );

  useEffect(() => {
    fetch("/api/profile")
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d) => {
        setContent(d.content ?? "");
        setStatus("ready");
      })
      .catch(() => setStatus("error"));
  }, []);

  async function save() {
    setStatus("saving");
    try {
      const res = await fetch("/api/profile", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      });
      setStatus(res.ok ? "saved" : "error");
    } catch {
      setStatus("error");
    }
  }

  return (
    <main className="about-wrap">
      <h1>About me</h1>
      <p className="about-help">
        Write what you want Jonny to always know — who you are, what you&rsquo;re
        working on, your preferences. This is used on every conversation, here
        and on the Mac.
      </p>
      {status === "loading" && <p className="status">loading…</p>}
      {status === "error" && (
        <p className="error">
          Can&rsquo;t reach the Mac. Make sure it&rsquo;s on and the tunnel is
          running.
        </p>
      )}
      {status !== "loading" && (
        <>
          <textarea
            value={content}
            onChange={(e) => {
              setContent(e.target.value);
              setStatus("ready");
            }}
            spellCheck
            rows={18}
          />
          <div className="about-actions">
            <a href="/" className="back-link">
              ← back
            </a>
            <button onClick={save} disabled={status === "saving"}>
              {status === "saving" ? "saving…" : status === "saved" ? "saved ✓" : "save"}
            </button>
          </div>
        </>
      )}
    </main>
  );
}
