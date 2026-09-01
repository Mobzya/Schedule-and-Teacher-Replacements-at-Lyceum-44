from __future__ import annotations

from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any

from ..config import Settings
from ..db import Database
from ..domain import EffectiveLesson, PublicationState

if TYPE_CHECKING:
    from .calendar import CalendarService


def _parse_time(value: str | None) -> time | None:
    if not value:
        return None
    return time.fromisoformat(value)


class ScheduleService:
    def __init__(self, db: Database, settings: Settings, calendar: CalendarService):
        self.db = db
        self.settings = settings
        self.calendar = calendar

    def bell_times_for_class(self, class_name: str) -> dict[int, tuple[time, time]] | None:
        schedule_name = self.settings.class_bell_schedules.get(class_name, "main")
        raw = self.settings.bell_schedules.get(schedule_name)
        if not raw:
            return None
        result: dict[int, tuple[time, time]] = {}
        for idx, pair in enumerate(raw, 1):
            if not isinstance(pair, list) or len(pair) != 2:
                return None
            result[idx] = (time.fromisoformat(pair[0]), time.fromisoformat(pair[1]))
        return result

    async def status(self) -> PublicationState:
        return await self.db.publication_state()

    async def day_schedule(self, class_name: str, profile: str | None, day: date) -> dict[str, Any]:
        publication = await self.db.publication_state()
        if publication == PublicationState.WAITING:
            return {"state": "waiting", "message": await self.db.waiting_message(), "lessons": []}
        if publication == PublicationState.PAUSED:
            return {"state": "paused", "message": "Расписание временно скрыто администратором.", "lessons": []}

        is_school, schedule_weekday, note = await self.calendar.school_day_info(day)
        if not is_school:
            return {"state": "day_off", "message": note or "Сегодня занятий нет.", "lessons": []}

        bell_times = self.bell_times_for_class(class_name)
        async with self.db.connect() as db:
            # Однодневные источники имеют приоритет над недельными. Внутри типа выбирается source priority.
            cur = await db.execute(
                """SELECT dl.*,ds.priority FROM dated_lessons dl JOIN data_sources ds ON ds.id=dl.source_id
                   WHERE ds.enabled=1 AND dl.lesson_date=? AND dl.class_name=? AND dl.profile IS NOT DISTINCT FROM ?
                     AND (ds.valid_from IS NULL OR ds.valid_from<=?) AND (ds.valid_until IS NULL OR ds.valid_until>=?)
                   ORDER BY ds.priority DESC, ds.id DESC""",
                (day.isoformat(), class_name, profile, day.isoformat(), day.isoformat()),
            )
            dated = await cur.fetchall()
            selected: dict[int, Any] = {}
            for r in dated:
                selected.setdefault(int(r["lesson_number"]), r)

            if not selected and schedule_weekday is not None:
                cur = await db.execute(
                    """SELECT wl.*,ds.priority FROM weekly_lessons wl JOIN data_sources ds ON ds.id=wl.source_id
                       WHERE ds.enabled=1 AND wl.weekday=? AND wl.class_name=? AND wl.profile IS NOT DISTINCT FROM ?
                         AND (ds.valid_from IS NULL OR ds.valid_from<=?) AND (ds.valid_until IS NULL OR ds.valid_until>=?)
                       ORDER BY ds.priority DESC, ds.id DESC""",
                    (schedule_weekday, class_name, profile, day.isoformat(), day.isoformat()),
                )
                for r in await cur.fetchall():
                    selected.setdefault(int(r["lesson_number"]), r)

            cur = await db.execute(
                """SELECT r.*,ds.priority FROM replacements r JOIN data_sources ds ON ds.id=r.source_id
                   WHERE ds.enabled=1 AND r.lesson_date=? AND r.class_name=?
                   ORDER BY ds.priority DESC,ds.id DESC""",
                (day.isoformat(), class_name),
            )
            repl: dict[int, Any] = {}
            for r in await cur.fetchall():
                repl.setdefault(int(r["lesson_number"]), r)

        if not selected and not repl:
            return {
                "state": "no_class_data",
                "message": "Для этого класса на выбранную дату расписание пока не загружено.",
                "lessons": [],
            }

        lessons: list[EffectiveLesson] = []
        all_nums = sorted(set(selected) | set(repl))
        for num in all_nums:
            base = selected.get(num)
            change = repl.get(num)
            if base is None and change is None:
                continue
            subject = (base["subject"] if base else "Дополнительный урок") or "Урок"
            room = (base["room"] if base else "") or ""
            teacher = base["teacher"] if base else None
            start = _parse_time(base["start_time"]) if base else None
            end = _parse_time(base["end_time"]) if base else None
            if start is None and bell_times and num in bell_times:
                start, end = bell_times[num]
            changed = False
            cancelled = False
            if change:
                changed = True
                cancelled = bool(change["cancelled"])
                if change["subject"]:
                    subject = change["subject"]
                if change["room"]:
                    room = change["room"]
                if change["teacher"]:
                    teacher = change["teacher"]
            lessons.append(EffectiveLesson(num, subject, room, teacher, start, end, changed, cancelled))

        return {"state": "ready", "message": note, "lessons": lessons, "bells_known": bell_times is not None}

    async def current_and_next(
        self, class_name: str, profile: str | None, now: datetime
    ) -> tuple[EffectiveLesson | None, EffectiveLesson | None, dict]:
        info = await self.day_schedule(class_name, profile, now.date())
        if info["state"] != "ready":
            return None, None, info
        lessons = [x for x in info["lessons"] if not x.cancelled]
        current = next(
            (x for x in lessons if x.start_time and x.end_time and x.start_time <= now.time() < x.end_time), None
        )
        future = [x for x in lessons if x.start_time and x.start_time > now.time()]
        next_lesson = min(future, key=lambda x: x.start_time) if future else None
        return current, next_lesson, info
