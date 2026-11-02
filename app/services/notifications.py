from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date
from typing import TYPE_CHECKING

from ..config import Settings
from ..db import Database
from ..domain import PublicationState
from ..messaging import send_chunks
from ..ui import fit_lines, lesson_line
from ..utils import now_local
from .calendar import CalendarService
from .schedule import ScheduleService

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from aiogram import Bot


class NotificationService:
    def __init__(self, db: Database, settings: Settings, schedules: ScheduleService):
        self.db = db
        self.settings = settings
        self.schedules = schedules
        self.calendar = CalendarService(db)

    async def tick(self, bot: Bot, user_limit: int | None = None) -> None:
        if await self.db.publication_state() != PublicationState.READY:
            await self.dispatch_outbox(bot, limit=min(user_limit or 20, 20))
            return
        now = now_local(self.settings.timezone)
        if not (await self.calendar.school_day_info(now.date()))[0]:
            # Do not scan all pupils or render schedules during school breaks.
            # Outbox still processes administrative broadcasts and drops stale notices.
            await self.dispatch_outbox(bot, limit=min(user_limit or 20, 20))
            return
        if user_limit is None:
            users = await self.db.all_users(notification_year=self.settings.academic_year)
            cursor = None
        else:
            cursor = int(await self.db.get_state("notification_cursor", "0") or 0)
            users = await self.db.all_users(
                after_user_id=cursor, limit=user_limit, notification_year=self.settings.academic_year
            )
            if not users and cursor:
                cursor = 0
                users = await self.db.all_users(
                    after_user_id=cursor, limit=user_limit, notification_year=self.settings.academic_year
                )
            # Persist progress before processing; a hard Worker CPU timeout must not
            # leave the cursor stuck on one user forever. Failed users return next cycle.
            await self.db.set_state("notification_cursor", str(users[-1]["user_id"]) if users else "0")
        cache = {}
        for u in users:
            if u["academic_year"] != self.settings.academic_year or not u["class_name"]:
                continue
            try:
                key = (u["class_name"], u["profile"])
                if key not in cache:
                    cache[key] = await self.schedules.day_schedule(*key, now.date())
                info = cache[key]
                if info["state"] != "ready":
                    continue
                if u["notifications"] and info.get("bells_known", True):
                    await self._lesson_reminder(u, info, now)
                if u["morning_summary"]:
                    await self._morning(u, info, now)
                if u["replacement_notifications"]:
                    await self._changes(u, info, now)
            except Exception:
                logger.exception("Notification tick failed for user %s", u["user_id"])
        await self.dispatch_outbox(bot, limit=min(user_limit or 20, 20))

    async def _queue(self, user_id: int, key: str, text: str) -> None:
        if not await self.db.notification_sent(user_id, key):
            await self.db.enqueue_notification(user_id, key, text)

    async def _lesson_reminder(self, u, info: dict, now) -> None:
        minutes = int(u["reminder_minutes"])
        for lesson in info["lessons"]:
            if lesson.cancelled or not lesson.start_time:
                continue
            start = now.replace(hour=lesson.start_time.hour, minute=lesson.start_time.minute, second=0, microsecond=0)
            delta = (start - now).total_seconds() / 60
            if 0 <= delta <= minutes:
                key = f"lesson:{now.date().isoformat()}:{lesson.lesson_number}:{minutes}"
                text = f"🔔 <b>Скоро {lesson.lesson_number}-й урок</b>\n\n{lesson_line(lesson)}\n\nДо начала примерно {max(0, int(round(delta)))} мин."
                await self._queue(int(u["user_id"]), key, text)

    async def _morning(self, u, info: dict, now) -> None:
        if not (6 <= now.hour <= 8):
            return
        key = f"morning:{now.date().isoformat()}"
        lessons = [x for x in info["lessons"] if not x.cancelled]
        if not lessons:
            return
        text = fit_lines(["🌅 <b>Расписание на сегодня</b>", "", *[lesson_line(x) for x in lessons]])
        await self._queue(int(u["user_id"]), key, text)

    async def _changes(self, u, info: dict, now) -> None:
        changed = [lesson for lesson in info["lessons"] if lesson.changed]
        if not changed or all(lesson.end_time and lesson.end_time <= now.time() for lesson in changed):
            return
        fingerprint = hashlib.sha1(
            repr(
                [
                    (lesson.lesson_number, lesson.subject, lesson.room, lesson.teacher, lesson.cancelled)
                    for lesson in changed
                ]
            ).encode()
        ).hexdigest()[:12]
        key = f"change:{now.date().isoformat()}:{fingerprint}"
        text = fit_lines(["⚡ <b>Изменение в расписании на сегодня</b>", "", *[lesson_line(x) for x in changed]])
        await self._queue(int(u["user_id"]), key, text)

    async def dispatch_outbox(self, bot: Bot, limit: int = 1) -> None:
        """Send queued notices; retry temporary delivery failures from D1."""
        today = now_local(self.settings.timezone).date()
        school_days = {}
        publication = None
        for item in await self.db.pending_notifications(limit):
            user_id = int(item["user_id"])
            key = item["notification_key"]
            kind, _, rest = key.partition(":")
            if kind in {"lesson", "morning", "change"}:
                try:
                    target_day = date.fromisoformat(rest.partition(":")[0])
                except ValueError:
                    target_day = None
                if target_day != today:
                    await self.db.discard_notification(user_id, key)
                    continue
                if publication is None:
                    publication = await self.db.publication_state()
                user = await self.db.get_user(user_id)
                if not user or not user["class_name"] or user["academic_year"] != self.settings.academic_year:
                    await self.db.discard_notification(user_id, key)
                    continue
                class_name = user["class_name"]
                if class_name not in school_days:
                    school_days[class_name] = (await self.calendar.school_day_info(today, class_name))[0]
                if publication != PublicationState.READY or not school_days[class_name]:
                    await self.db.discard_notification(user_id, key)
                    continue
            try:
                await send_chunks(bot, user_id, item["message"])
            except Exception as exc:
                if getattr(exc, "error_code", None) == 403 or "bot was blocked" in str(exc).lower():
                    await self.db.discard_notification(user_id, key)
                    continue
                retry_after = getattr(exc, "retry_after", None)
                if retry_after is not None:
                    await self.db.retry_notification(user_id, key, str(exc), int(float(retry_after)) + 1)
                    continue
                logger.warning("Notification delivery failed for %s: %s", user_id, exc)
                retry_delay = min(3600, 60 * (2 ** min(int(item["attempts"]), 6)))
                await self.db.retry_notification(user_id, key, str(exc), retry_delay)
            else:
                await self.db.finish_notification(user_id, key)

    async def worker(self, bot: Bot) -> None:
        while True:
            try:
                await self.tick(bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Notification worker error")
            await asyncio.sleep(self.settings.notification_tick_seconds)
