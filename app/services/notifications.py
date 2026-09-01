from __future__ import annotations

import asyncio
import hashlib
import logging

from aiogram import Bot

from ..config import Settings
from ..db import Database
from ..domain import PublicationState
from ..messaging import send_chunks
from ..ui import lesson_line
from ..utils import now_local
from .schedule import ScheduleService

logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(self, db: Database, settings: Settings, schedules: ScheduleService):
        self.db = db
        self.settings = settings
        self.schedules = schedules

    async def tick(self, bot: Bot) -> None:
        if await self.db.publication_state() != PublicationState.READY:
            return
        now = now_local(self.settings.timezone)
        users = await self.db.all_users()
        for u in users:
            if u["academic_year"] != self.settings.academic_year or not u["class_name"]:
                continue
            try:
                info = await self.schedules.day_schedule(u["class_name"], u["profile"], now.date())
                if info["state"] != "ready":
                    continue
                if u["notifications"] and info.get("bells_known", True):
                    await self._lesson_reminder(bot, u, info, now)
                if u["morning_summary"]:
                    await self._morning(bot, u, info, now)
                if u["replacement_notifications"]:
                    await self._changes(bot, u, info, now)
            except Exception:
                logger.exception("Notification tick failed for user %s", u["user_id"])

    async def _lesson_reminder(self, bot: Bot, u, info: dict, now) -> None:
        minutes = int(u["reminder_minutes"])
        for lesson in info["lessons"]:
            if lesson.cancelled or not lesson.start_time:
                continue
            start = now.replace(hour=lesson.start_time.hour, minute=lesson.start_time.minute, second=0, microsecond=0)
            delta = (start - now).total_seconds() / 60
            if 0 <= delta <= minutes:
                key = f"lesson:{now.date().isoformat()}:{lesson.lesson_number}:{minutes}"
                if await self.db.notification_sent(u["user_id"], key):
                    continue
                text = f"🔔 <b>Скоро {lesson.lesson_number}-й урок</b>\n\n{lesson_line(lesson)}\n\nДо начала примерно {max(0, int(round(delta)))} мин."
                await send_chunks(bot, int(u["user_id"]), text)
                await self.db.mark_notification(u["user_id"], key)

    async def _morning(self, bot: Bot, u, info: dict, now) -> None:
        if not (6 <= now.hour <= 8):
            return
        key = f"morning:{now.date().isoformat()}"
        if await self.db.notification_sent(u["user_id"], key):
            return
        lessons = [x for x in info["lessons"] if not x.cancelled]
        if not lessons:
            return
        text = "🌅 <b>Расписание на сегодня</b>\n\n" + "\n".join(lesson_line(x) for x in lessons)
        await send_chunks(bot, int(u["user_id"]), text)
        await self.db.mark_notification(u["user_id"], key)

    async def _changes(self, bot: Bot, u, info: dict, now) -> None:
        for lesson in info["lessons"]:
            if not lesson.changed:
                continue
            fingerprint = hashlib.sha1(
                f"{lesson.lesson_number}|{lesson.subject}|{lesson.room}|{lesson.teacher}|{lesson.cancelled}".encode()
            ).hexdigest()[:12]
            key = f"change:{now.date().isoformat()}:{lesson.lesson_number}:{fingerprint}"
            if await self.db.notification_sent(u["user_id"], key):
                continue
            text = f"⚡ <b>Изменение в расписании на сегодня</b>\n\n{lesson_line(lesson)}"
            await send_chunks(bot, int(u["user_id"]), text)
            await self.db.mark_notification(u["user_id"], key)

    async def worker(self, bot: Bot) -> None:
        while True:
            try:
                await self.tick(bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Notification worker error")
            await asyncio.sleep(self.settings.notification_tick_seconds)
