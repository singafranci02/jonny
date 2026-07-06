import Anthropic from "@anthropic-ai/sdk";
import { NextRequest, NextResponse } from "next/server";

// Middleware already gates this route behind the password cookie.

const SYSTEM_PROMPT = `You are Jonny, a friendly voice assistant living in a web dashboard as a hovering ball of light.

Your replies are spoken aloud by the browser's speech synthesis, so:
- Answer in short, natural, conversational sentences.
- No markdown, no lists, no code blocks, no emoji — plain speakable prose only.
- Default to one to three sentences. Go longer only when the question genuinely needs it.
- If you don't know something, say so plainly instead of guessing.

Be direct, warm, and a little playful.`;

const client = new Anthropic();

type ChatMessage = { role: "user" | "assistant"; content: string };

export async function POST(request: NextRequest) {
  const body = await request.json().catch(() => null);
  const messages: ChatMessage[] = Array.isArray(body?.messages) ? body.messages : [];
  if (
    messages.length === 0 ||
    !messages.every(
      (m) =>
        (m.role === "user" || m.role === "assistant") &&
        typeof m.content === "string" &&
        m.content.length < 8000,
    )
  ) {
    return NextResponse.json({ error: "bad request" }, { status: 400 });
  }

  try {
    const response = await client.messages.create({
      model: "claude-sonnet-5",
      max_tokens: 512,
      thinking: { type: "disabled" },
      system: [
        {
          type: "text",
          text: SYSTEM_PROMPT,
          cache_control: { type: "ephemeral" },
        },
      ],
      messages: messages.slice(-40),
    });
    const text = response.content
      .filter((block) => block.type === "text")
      .map((block) => block.text)
      .join("")
      .trim();
    return NextResponse.json({
      text: text || "Hmm, I came up empty on that one.",
      usage: {
        input: response.usage.input_tokens,
        output: response.usage.output_tokens,
      },
    });
  } catch (err) {
    console.error("chat error", err);
    return NextResponse.json({ error: "model call failed" }, { status: 502 });
  }
}
