"""HTTP API for the website's Life HQ pages (proxied by Vercel, bearer-token
protected like every other brain endpoint)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .clock import utc_now


class ReminderIn(BaseModel):
    area: str
    text: str
    due: str  # local "YYYY-MM-DDTHH:mm"
    recurrence: str = "none"


class ItemIn(BaseModel):
    area: str
    kind: str = "task"
    title: str
    due_date: str | None = None


class LogIn(BaseModel):
    area: str
    metric: str
    value: float | None = None
    note: str | None = None


class AskIn(BaseModel):
    text: str


def make_router(get_service: Callable, require_token: Callable) -> APIRouter:
    router = APIRouter(prefix="/life", dependencies=[Depends(require_token)])

    def svc():
        s = get_service()
        if s is None:
            raise HTTPException(503, "the life organiser isn't running (check the life: section in config.yaml)")
        return s

    def area_ok(s, area: str) -> str:
        if area not in s.labels:
            raise HTTPException(400, f"unknown area {area!r}")
        return area

    @router.get("/dashboard")
    async def dashboard() -> dict:
        s = svc()
        return await asyncio.to_thread(s.dashboard)

    @router.get("/status")
    async def status() -> dict:
        s = svc()
        return await asyncio.to_thread(s.status_dict)

    @router.post("/reminders")
    async def add_reminder(body: ReminderIn) -> dict:
        s = svc()
        due = s.clock.parse(body.due)
        if due is None:
            raise HTTPException(400, "couldn't read that time")
        try:
            r = await asyncio.to_thread(
                s.store.add_reminder, area_ok(s, body.area), body.text, due, body.recurrence or "none"
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"id": r["id"]}

    @router.post("/reminders/{rid}/{op}")
    async def reminder_op(rid: int, op: str) -> dict:
        s = svc()
        if op == "done":
            r = await asyncio.to_thread(s.store.complete, rid)
        elif op == "cancel":
            r = await asyncio.to_thread(lambda: s.store.update_reminder(rid, status="cancelled"))
        elif op == "snooze":
            r = await asyncio.to_thread(s.store.snooze, rid, utc_now() + timedelta(hours=1))
        else:
            raise HTTPException(400, f"unknown action {op!r}")
        if r is None:
            raise HTTPException(404, "no such reminder")
        return {"ok": True}

    @router.post("/items")
    async def add_item(body: ItemIn) -> dict:
        s = svc()
        due = s.clock.parse_date(body.due_date) if body.due_date else None
        if body.due_date and due is None:
            raise HTTPException(400, "couldn't read that date")
        if not body.title.strip():
            raise HTTPException(400, "title is required")
        try:
            i = await asyncio.to_thread(s.store.add_item, area_ok(s, body.area), body.kind, body.title, None, 2, due)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"id": i["id"]}

    @router.post("/items/{iid}/{op}")
    async def item_op(iid: int, op: str) -> dict:
        s = svc()
        status = {"done": "done", "archive": "archived", "reopen": "open"}.get(op)
        if status is None:
            raise HTTPException(400, f"unknown action {op!r}")
        if await asyncio.to_thread(lambda: s.store.update_item(iid, status=status)) is None:
            raise HTTPException(404, "no such item")
        return {"ok": True}

    @router.post("/logs")
    async def add_log(body: LogIn) -> dict:
        s = svc()
        try:
            entry = await asyncio.to_thread(
                s.store.add_log, area_ok(s, body.area), body.metric, body.value, None, body.note
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"id": entry["id"]}

    @router.post("/ask")
    async def ask(body: AskIn) -> dict:
        s = svc()
        text = body.text.strip()
        if not text:
            raise HTTPException(400, "empty message")
        return {"text": await s.chat(text, "dashboard")}

    @router.post("/test-telegram")
    async def test_telegram() -> dict:
        s = svc()
        if not s.can_message():
            raise HTTPException(400, "Telegram isn't configured on the Mac yet (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env)")
        await s.notify("👋 Jarvis here. Telegram is connected. Send /help to see what I can do.", "system")
        return {"ok": True}

    @router.post("/checkins/{key}/run")
    async def run_checkin(key: str) -> dict:
        s = svc()
        ci = s.checkin_by_key(key)
        if ci is None:
            raise HTTPException(404, "no such check-in")
        if not s.can_message():
            raise HTTPException(400, "Telegram isn't configured on the Mac yet")
        asyncio.ensure_future(s._run_checkin_safe(ci))
        return {"started": True}

    return router
