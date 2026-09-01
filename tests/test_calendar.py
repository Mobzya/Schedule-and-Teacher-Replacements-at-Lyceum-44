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
