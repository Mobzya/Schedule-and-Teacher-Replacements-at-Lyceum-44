from __future__ import annotations

from datetime import date

import holidays

from ..db import Database


class CalendarService:
    def __init__(self, db: Database):
        self.db = db
        self.ru_holidays = holidays.RU()

    async def school_day_info(self, day: date) -> tuple[bool, int | None, str | None]:
        override = await self.db.calendar_override(day)
        if override:
            return bool(override["is_school_day"]), override["schedule_weekday"], override["note"]
        if day in self.ru_holidays:
            return False, None, self.ru_holidays.get(day)
        if day.weekday() >= 5:
            return False, None, "Выходной"
        return True, day.weekday(), None
