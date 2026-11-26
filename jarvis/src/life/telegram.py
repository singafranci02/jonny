"""Minimal Telegram Bot API client (async httpx).

The Mac long-polls Telegram (getUpdates), so no webhook, tunnel or cloud
service is involved, and if the Mac is off Telegram simply holds messages
(for up to 24 hours) until it is back.
"""

from __future__ import annotations

import asyncio

import httpx

MAX_LEN = 4000  # Telegram's hard limit is 4096 characters per message
RETRY_DELAYS = (2, 5, 15)  # seconds between send attempts on transient failures


class TelegramError(Exception):
    def __init__(self, description: str, code: int | None = None, retry_after: int | None = None):
        super().__init__(description)
        self.code = code
        self.retry_after = retry_after


def chunks(text: str, size: int = MAX_LEN) -> list[str]:
    """Split long text on line breaks where possible; never loses text."""
    text = (text or "").strip() or "(empty)"
    out: list[str] = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text:
        out.append(text)
    return out


def reminder_keyboard(rid: int) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Done", "callback_data": f"done:{rid}"},
            {"text": "⏰ +1h", "callback_data": f"snooze:{rid}:60"},
            {"text": "🌙 Tomorrow", "callback_data": f"snooze:{rid}:tomorrow"},
        ]]
    }


class TelegramClient:
    def __init__(self, token: str):
        self._base = f"https://api.telegram.org/bot{token}/"
        # long polls hold the connection ~50s; give reads room beyond that
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(75.0, connect=15.0))

    async def call(self, method: str, **params):
        resp = await self._http.post(self._base + method, json={k: v for k, v in params.items() if v is not None})
        try:
            data = resp.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {resp.status_code}", resp.status_code)
        if not data.get("ok"):
            raise TelegramError(
                f"{method}: {data.get('description', 'failed')}",
                data.get("error_code"),
                (data.get("parameters") or {}).get("retry_after"),
            )
        return data.get("result")

    async def _send_message(self, **params):
        """sendMessage with retries for transient failures: network errors,
        Telegram 5xx, and 429 rate limits (waiting as long as Telegram asks)."""
        for attempt in range(len(RETRY_DELAYS) + 1):
            last_try = attempt == len(RETRY_DELAYS)
            try:
                return await self.call("sendMessage", **params)
            except TelegramError as e:
                transient = e.code == 429 or (e.code or 0) >= 500
                if not transient or last_try:
                    raise
                await asyncio.sleep(e.retry_after or RETRY_DELAYS[attempt])
            except httpx.TransportError:
                if last_try:
                    raise
                await asyncio.sleep(RETRY_DELAYS[attempt])

    async def send(self, chat_id: int, text: str, keyboard: dict | None = None) -> int | None:
        """Plain text (no parse_mode, so agent text can't break formatting).
        Buttons go on the last chunk."""
        parts = chunks(text)
        last = None
        for i, part in enumerate(parts):
            msg = await self._send_message(
                chat_id=chat_id,
                text=part,
                link_preview_options={"is_disabled": True},
                reply_markup=keyboard if i == len(parts) - 1 else None,
            )
            last = msg.get("message_id") if isinstance(msg, dict) else None
        return last

    async def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        return await self.call(
            "getUpdates", offset=offset, timeout=timeout, allowed_updates=["message", "callback_query"]
        ) or []

    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def delete_webhook(self) -> None:
        await self.call("deleteWebhook", drop_pending_updates=False)

    async def typing(self, chat_id: int) -> None:
        try:
            await self.call("sendChatAction", chat_id=chat_id, action="typing")
        except Exception:
            pass

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        try:
            await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except Exception:
            pass

    async def edit_text(self, chat_id: int, message_id: int, text: str, keyboard: dict | None = None) -> None:
        """Replace a message's text (and its inline buttons, when given)."""
        try:
            await self.call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text[:MAX_LEN],
                link_preview_options={"is_disabled": True},
                reply_markup=keyboard,
            )
        except Exception:
            pass  # e.g. "message is not modified" after a repeated tap

    async def edit_keyboard(self, chat_id: int, message_id: int, keyboard: dict) -> None:
        """Swap only the inline buttons (the text stays as it is)."""
        try:
            await self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup=keyboard)
        except Exception:
            pass

    async def close(self) -> None:
        await self._http.aclose()
