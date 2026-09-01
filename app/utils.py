from __future__ import annotations

import html
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

RU_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def esc(value: object | None) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def normalize_class_name(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().lower()).replace("ё", "е")


def now_local(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def today_local(tz: ZoneInfo) -> date:
    return now_local(tz).date()


def academic_year_for(day: date) -> str:
    start = day.year if day.month >= 8 else day.year - 1
    return f"{start}-{start + 1}"
