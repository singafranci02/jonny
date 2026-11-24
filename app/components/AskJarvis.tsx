"use client";

import { useActionState } from "react";
import { askJarvisAction, type AskState } from "../actions";

const EMPTY: AskState = { question: "", reply: "", error: "" };

export function AskJarvis() {
  const [state, action, pending] = useActionState(askJarvisAction, EMPTY);
  return (
    <div className="ask-wrap">
      <form action={action} className="ask">
        <input name="text" placeholder="Ask Jarvis…" autoComplete="off" disabled={pending} />
        <button type="submit" disabled={pending}>
          {pending ? "Thinking…" : "Send"}
        </button>
      </form>
      {pending && <p className="ask-note">Jarvis may bring in an area agent; this can take a minute.</p>}
      {!pending && state.reply && (
        <p className="ask-reply">
          <span className="muted">you: {state.question}</span>
          {state.reply}
        </p>
      )}
      {!pending && state.error && <p className="ask-reply error">{state.error}</p>}
    </div>
  );
}
