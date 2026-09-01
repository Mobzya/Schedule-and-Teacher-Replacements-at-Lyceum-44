from datetime import date
from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.domain import ParsedLesson, ParsedReplacement, PublicationState, SourceType
from app.services.schedule import ScheduleService


class FakeCalendar:
    async def school_day_info(self, day):
        return True, day.weekday(), None


def settings(path):
    return Settings(
        "",
        frozenset(),
        "Europe/Moscow",
        "2026-2027",
        path,
        10000,
        10,
        30,
        15,
        {},
        {},
        {"main": [["08:15", "09:00"], ["09:15", "10:00"]]},
        {},
    )


@pytest.mark.asyncio
async def test_waiting_hides_snapshots_and_date_overrides_weekly(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    await db.init()
    st = settings(tmp_path / "db.sqlite")
    weekly = await db.add_source("w", SourceType.WEEKLY, "x", 100, None, None, "2026-2027", 1)
    dated = await db.add_source("d", SourceType.DATE, "x", 500, date(2026, 9, 7), date(2026, 9, 7), "2026-2027", 1)
    repl = await db.add_source("r", SourceType.REPLACEMENTS, "x", 100, None, None, "2026-2027", 1)
    await db.replace_source_snapshot(
        weekly, SourceType.WEEKLY, [ParsedLesson("10а", None, 0, None, 1, "Алгебра", "316")], []
    )
    await db.replace_source_snapshot(
        dated, SourceType.DATE, [ParsedLesson("10а", None, None, date(2026, 9, 7), 1, "Физика", "301")], []
    )
    await db.replace_source_snapshot(
        repl, SourceType.REPLACEMENTS, [], [ParsedReplacement(date(2026, 9, 7), "10а", 1, None, "Иванов", "305")]
    )
    svc = ScheduleService(db, st, FakeCalendar())
    waiting = await svc.day_schedule("10а", None, date(2026, 9, 7))
    assert waiting["state"] == "waiting"
    await db.set_publication_state(PublicationState.READY, 1)
    ready = await svc.day_schedule("10а", None, date(2026, 9, 7))
    assert ready["state"] == "ready"
    lesson = ready["lessons"][0]
    assert lesson.subject == "Физика" and lesson.room == "305" and lesson.changed
    assert lesson.start_time.strftime("%H:%M") == "08:15"
