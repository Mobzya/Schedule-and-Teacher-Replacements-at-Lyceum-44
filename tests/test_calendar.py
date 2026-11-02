from datetime import date
from pathlib import Path

import pytest

from app.db import Database
from app.services.calendar import CalendarService


@pytest.mark.asyncio
async def test_calendar_weekend_and_admin_override(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    await db.init()
    calendar = CalendarService(db)
    saturday = date(2026, 9, 5)

    assert await calendar.school_day_info(saturday) == (False, None, "Выходной")

    await db.set_calendar_day(saturday, True, 0, "Учимся по понедельнику", 1)
    assert await calendar.school_day_info(saturday) == (True, 0, "Учимся по понедельнику")

    await db.clear_calendar_day(saturday)
    assert await calendar.school_day_info(saturday) == (False, None, "Выходной")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "day,class_name,is_school,reason",
    [
        ("2026-10-23", "10а", True, None),
        ("2026-10-24", "10а", False, "Осенние"),
        ("2026-11-01", "10а", False, "Осенние"),
        ("2026-11-02", "10а", True, None),
        ("2026-11-04", "10а", False, None),
        ("2026-12-25", "10а", True, None),
        ("2026-12-26", "10а", False, "Зимние"),
        ("2027-01-10", "10а", False, "Зимние"),
        ("2027-01-11", "10а", True, None),
        ("2027-02-22", "10а", False, "23 февраля"),
        ("2027-02-23", "10а", False, None),
        ("2027-02-24", "10а", True, None),
        ("2027-02-24", "1а", False, "первых классов"),
        ("2027-02-24", "11а", True, None),
        ("2027-02-24", None, True, None),
        ("2027-03-01", "1а", True, None),
        ("2027-03-08", "10а", False, None),
        ("2027-03-19", "10а", True, None),
        ("2027-03-22", "10а", False, "Весенние"),
        ("2027-03-28", "10а", False, "Весенние"),
        ("2027-03-29", "10а", True, None),
        ("2027-05-03", "10а", False, "1 мая"),
        ("2027-05-10", "10а", False, "9 мая"),
        ("2027-05-26", "10а", True, None),
        ("2027-05-27", "10а", False, "Летние"),
        ("2027-06-01", "9а", False, "ГИА"),
        ("2027-06-01", "11а", False, "ГИА"),
        ("2027-08-31", "10а", False, "Летние"),
        ("2027-09-01", "10а", True, None),
        ("2027-10-25", "10а", True, None),  # no invented 2027/28 vacations
    ],
)
async def test_official_school_calendar(tmp_path, day, class_name, is_school, reason):
    db = Database(tmp_path / "calendar.sqlite")
    await db.init()
    actual, weekday, note = await CalendarService(db).school_day_info(date.fromisoformat(day), class_name)
    assert actual is is_school
    assert weekday == (date.fromisoformat(day).weekday() if is_school else None)
    if reason:
        assert reason in note


@pytest.mark.asyncio
async def test_override_has_priority_over_official_vacation(tmp_path):
    db = Database(tmp_path / "override.sqlite")
    await db.init()
    calendar = CalendarService(db)
    day = date(2026, 10, 26)
    await db.set_calendar_day(day, True, 2, "Разовое занятие по среде", 1)
    assert await calendar.school_day_info(day, "10а") == (True, 2, "Разовое занятие по среде")
    await db.clear_calendar_day(day)
    assert await calendar.school_day_info(day, "10а") == (False, None, "Осенние каникулы")
