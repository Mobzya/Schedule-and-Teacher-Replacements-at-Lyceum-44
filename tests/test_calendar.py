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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "day,class_name,phrase",
    [
        ("2026-11-04", "10а", "с наступающим Днём народного единства"),
        ("2027-01-01", "10а", "с наступающим Новым годом"),
        ("2027-01-07", "10а", "с наступающим Рождеством"),
        ("2027-02-23", "10а", "с наступающим Днём защитника Отечества"),
        ("2027-03-08", "10а", "с наступающим Международным женским днём"),
        ("2027-05-01", "10а", "с наступающим Праздником Весны и Труда"),
        ("2027-05-09", "10а", "с наступающим Днём Победы"),
        ("2027-06-12", "10а", "с наступающим Днём России"),
        ("2027-06-24", "10а", "с наступающим Днём Чувашской Республики"),
        ("2026-10-24", "10а", "с началом осенних каникул"),
        ("2026-12-26", "10а", "с началом зимних каникул"),
        ("2027-03-22", "10а", "с началом весенних каникул"),
        ("2027-05-27", "10а", "с началом летних каникул"),
        ("2027-02-20", "1а", "с началом дополнительных каникул"),
        ("2027-05-27", "9а", "Удачи на экзаменах"),
        ("2027-05-27", "11а", "Удачи на экзаменах"),
    ],
)
async def test_day_off_notice_uses_correct_russian_forms_and_class_calendar(tmp_path, day, class_name, phrase):
    db = Database(tmp_path / "greetings.sqlite")
    await db.init()
    text = await CalendarService(db).day_off_notice(date.fromisoformat(day), class_name)
    assert "Завтра" in text and "уроков нет" in text
    assert phrase in text
    assert len(text) < 200
    assert len(text.splitlines()) == 2
    if class_name in {"9а", "11а"}:
        assert "каникул" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "day,class_name",
    [
        ("2026-11-03", "10а"),  # ordinary school day
        ("2026-11-07", "10а"),  # ordinary Saturday
        ("2026-10-05", "10а"),  # Teacher's Day still has lessons
        ("2026-10-25", "10а"),  # vacation has already started
        ("2027-01-02", "10а"),  # no daily New Year congratulations
        ("2027-02-22", "10а"),  # transferred day off, not the holiday itself
        ("2027-02-20", "11а"),  # extra vacation is for first grade only
    ],
)
async def test_day_off_notice_is_limited_to_first_vacation_day_and_actual_holidays(tmp_path, day, class_name):
    db = Database(tmp_path / "no-greetings.sqlite")
    await db.init()
    assert await CalendarService(db).day_off_notice(date.fromisoformat(day), class_name) is None


@pytest.mark.asyncio
async def test_day_off_notice_respects_schoolday_override(tmp_path):
    db = Database(tmp_path / "schoolday.sqlite")
    await db.init()
    day = date(2026, 11, 4)
    await db.set_calendar_day(day, True, 2, "Учимся по среде", 1)
    assert await CalendarService(db).day_off_notice(day, "10а") is None
