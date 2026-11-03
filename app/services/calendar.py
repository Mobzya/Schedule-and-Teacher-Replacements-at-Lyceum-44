from __future__ import annotations

import re
from datetime import date

import holidays

from ..config import DEFAULT_ACADEMIC_YEAR
from ..db import Database

# Building holiday rules is CPU-heavy under Pyodide. Precompute the supported
# school year's dates once while Cloudflare snapshots imports, then reuse them.
_RU_HOLIDAYS = holidays.RU(
    years=[int(year) for year in DEFAULT_ACADEMIC_YEAR.split("-")],
    language="ru",
    expand=False,
)
# Verified 2026/27 school calendar; never extrapolate vacations into another year.
# Sources and the distinction between regular lessons and GIA: docs/school-calendar.md.
_BREAKS = (
    (date(2026, 10, 24), date(2026, 11, 1), "Осенние каникулы"),
    (date(2026, 12, 26), date(2027, 1, 10), "Зимние каникулы"),
    (date(2027, 3, 22), date(2027, 3, 28), "Весенние каникулы"),
    (date(2027, 5, 27), date(2027, 8, 31), "Летние каникулы"),
)
_FIRST_GRADE_BREAK = (date(2027, 2, 20), date(2027, 2, 28))
_BREAK_STARTS = frozenset(start for start, _, _ in _BREAKS) | {_FIRST_GRADE_BREAK[0]}
# Ready-made Russian forms avoid guessing the gender/case of a holiday name.
_BREAK_GREETINGS = {
    "Осенние каникулы": "Поздравляю с началом осенних каникул! 🍂",
    "Зимние каникулы": "Поздравляю с началом зимних каникул! ❄️",
    "Весенние каникулы": "Поздравляю с началом весенних каникул! 🌷",
    "Летние каникулы": "Поздравляю с началом летних каникул! ☀️",
}
# Only the occasion itself: no daily greetings throughout New Year holidays
# or on transferred/ordinary weekends. Holidays during a school break still count.
_HOLIDAY_GREETINGS = {
    (1, 1): "Поздравляю с наступающим Новым годом! 🎄",
    (1, 7): "Поздравляю с наступающим Рождеством! ✨",
    (2, 23): "Поздравляю с наступающим Днём защитника Отечества! 🎉",
    (3, 8): "Поздравляю с наступающим Международным женским днём! 🌷",
    (5, 1): "Поздравляю с наступающим Праздником Весны и Труда! 🌿",
    (5, 9): "Поздравляю с наступающим Днём Победы! 🕊️",
    (6, 12): "Поздравляю с наступающим Днём России! 🇷🇺",
    (6, 24): "Поздравляю с наступающим Днём Чувашской Республики! 🌸",
    (11, 4): "Поздравляю с наступающим Днём народного единства! 🇷🇺",
}
# The installed holidays release does not contain the approved 2027 transfers.
_ADDITIONAL_DAYS_OFF = {
    date(2026, 12, 31): "Перенесённый выходной",
    date(2027, 2, 22): "Перенесённый выходной к 23 февраля",
    date(2027, 5, 3): "Выходной за 1 мая",
    date(2027, 5, 10): "Выходной за 9 мая",
    date(2027, 6, 14): "Выходной за 12 июня",
    date(2027, 11, 5): "Перенесённый выходной",
    date(2027, 12, 31): "Перенесённый выходной",
    date(2026, 6, 24): "День Чувашской Республики",
    date(2027, 6, 24): "День Чувашской Республики",
}


def _grade(class_name: str | None) -> int | None:
    match = re.match(r"^(1[01]|[1-9])(?=\D|$)", (class_name or "").strip())
    return int(match[0]) if match else None


class CalendarService:
    def __init__(self, db: Database):
        self.db = db
        self.ru_holidays = _RU_HOLIDAYS

    async def school_day_info(self, day: date, class_name: str | None = None) -> tuple[bool, int | None, str | None]:
        override = await self.db.calendar_override(day)
        if override:
            return bool(override["is_school_day"]), override["schedule_weekday"], override["note"]
        for start, end, note in _BREAKS:
            if start <= day <= end:
                if start.month == 5 and _grade(class_name) in (9, 11):
                    note = "Регулярные уроки завершены. Экзамены — по отдельному расписанию ГИА."
                return False, None, note
        if _grade(class_name) == 1 and _FIRST_GRADE_BREAK[0] <= day <= _FIRST_GRADE_BREAK[1]:
            return False, None, "Дополнительные каникулы первых классов"
        if day in _ADDITIONAL_DAYS_OFF:
            return False, None, _ADDITIONAL_DAYS_OFF[day]
        if day in self.ru_holidays:
            return False, None, self.ru_holidays.get(day)
        if day.weekday() >= 5:
            return False, None, "Выходной"
        return True, day.weekday(), None

    @staticmethod
    def has_day_off_occasion(day: date) -> bool:
        """Cheap date check before scanning pupils in a Cloudflare Cron tick."""
        return day in _BREAK_STARTS or (day.month, day.day) in _HOLIDAY_GREETINGS

    async def day_off_notice(self, day: date, class_name: str) -> str | None:
        """Return a greeting only for a confirmed day off and a known occasion."""
        if not self.has_day_off_occasion(day) or (await self.school_day_info(day, class_name))[0]:
            return None
        greeting = None
        for start, _, name in _BREAKS:
            if day == start:
                if start.month == 5 and _grade(class_name) in (9, 11):
                    return (
                        "<b>Завтра регулярных уроков нет.</b>\n"
                        "Поздравляю с окончанием учебных занятий! Удачи на экзаменах 🍀"
                    )
                greeting = _BREAK_GREETINGS[name]
                break
        if day == _FIRST_GRADE_BREAK[0] and _grade(class_name) == 1:
            greeting = "Поздравляю с началом дополнительных каникул! Хорошего отдыха ❄️"
        greeting = greeting or _HOLIDAY_GREETINGS.get((day.month, day.day))
        return f"<b>Завтра уроков нет.</b>\n{greeting}" if greeting else None
