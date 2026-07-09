"use client";

import { useEffect, useState } from "react";

// Read-only view of the files Jonny has made in its workspace folder.
// Creation and editing happen by voice; this is just for looking.

type WsFile = { name: string; size: number };

export default function FilesPage() {
  const [files, setFiles] = useState<WsFile[]>([]);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [open, setOpen] = useState<string | null>(null);
  const [content, setContent] = useState("");

  useEffect(() => {
    fetch("/api/workspace")
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d) => {
        setFiles(d.files ?? []);
        setStatus("ready");
      })
      .catch(() => setStatus("error"));
  }, []);

  async function view(name: string) {
    if (open === name) {
      setOpen(null);
      return;
    }
    setOpen(name);
    setContent("loading…");
    try {
      const res = await fetch(`/api/workspace?name=${encodeURIComponent(name)}`);
      const d = await res.json();
      setContent(res.ok ? (d.content ?? "") : "couldn't load this file");
    } catch {
      setContent("couldn't load this file");
    }
  }

  return (
    <main className="about-wrap">
      <h1>Files</h1>
      <p className="about-help">
        What Jonny has written in its workspace. Ask it by voice to create or
        edit files — this page is read-only.
      </p>
      {status === "loading" && <p className="status">loading…</p>}
      {status === "error" && (
        <p className="error">
          Can&rsquo;t reach the Mac. Make sure it&rsquo;s on and the tunnel is
          running.
        </p>
      )}
      {status === "ready" && files.length === 0 && (
        <p className="status">Nothing yet — try &ldquo;save a note with…&rdquo;</p>
      )}
      <ul className="file-list">
        {files.map((f) => (
          <li key={f.name}>
            <button className="file-row" onClick={() => view(f.name)}>
              <span>{f.name}</span>
              <span className="file-size">{f.size} B</span>
            </button>
            {open === f.name && <pre className="file-view">{content}</pre>}
          </li>
        ))}
      </ul>
      <div className="about-actions">
        <a href="/" className="back-link">
          ← back
        </a>
      </div>
    </main>
  );
}
