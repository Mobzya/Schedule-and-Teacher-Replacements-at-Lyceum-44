from datetime import date
from pathlib import Path

import pytest

from app.db import Database
from app.domain import ParsedLesson, PublicationState, SourceType


@pytest.mark.asyncio
async def test_database_state_users_sources_and_snapshot(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    await db.init()
    assert await db.publication_state() == PublicationState.WAITING
    await db.upsert_user(1, "2026-2027", default_reminder=15)
    user = await db.get_user(1)
    assert user["user_id"] == 1 and user["class_name"] is None
    sid = await db.add_source(
        "weekly",
        SourceType.WEEKLY,
        "https://docs.google.com/spreadsheets/d/a/edit#gid=0",
        100,
        None,
        None,
        "2026-2027",
        1,
    )
    lessons = [ParsedLesson("10а", None, 0, None, 1, "Алгебра", "316")]
    await db.replace_source_snapshot(sid, SourceType.WEEKLY, lessons, [])
    assert ("10а", None) in await db.classes()
    await db.set_publication_state(PublicationState.READY, 1)
    assert await db.publication_state() == PublicationState.READY


@pytest.mark.asyncio
async def test_snapshot_replaces_old_data(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    await db.init()
    sid = await db.add_source(
        "day",
        SourceType.DATE,
        "https://docs.google.com/spreadsheets/d/a/edit#gid=0",
        500,
        date(2026, 9, 2),
        date(2026, 9, 2),
        "2026-2027",
        1,
    )
    a = [ParsedLesson("10а", None, None, date(2026, 9, 2), 1, "Алгебра", "316")]
    b = [ParsedLesson("10а", None, None, date(2026, 9, 2), 1, "Физика", "301")]
    await db.replace_source_snapshot(sid, SourceType.DATE, a, [])
    await db.replace_source_snapshot(sid, SourceType.DATE, b, [])
    async with db.connect() as c:
        rows = await (await c.execute("SELECT subject FROM dated_lessons")).fetchall()
    assert [r[0] for r in rows] == ["Физика"]


@pytest.mark.asyncio
async def test_new_academic_year_requires_class_selection_again(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    await db.init()
    await db.upsert_user(1, "2025-2026", "9а", "физмат", 15)

    await db.upsert_user(1, "2026-2027", default_reminder=15)

    user = await db.get_user(1)
    assert user["academic_year"] == "2026-2027"
    assert user["class_name"] is None
    assert user["profile"] is None


@pytest.mark.asyncio
async def test_unsafe_legacy_source_can_be_retired_without_deleting_it(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    await db.init()
    sid = await db.add_source(
        "legacy replacements",
        SourceType.REPLACEMENTS,
        "https://docs.google.com/spreadsheets/d/unsafe-id/edit",
        10,
        None,
        None,
        "2026-2027",
        1,
    )

    assert await db.disable_sources_by_url_fragment("unsafe-id", "ambiguous year") == 1
    source = await db.get_source(sid)
    assert not source["enabled"]
    assert source["last_status"] == "stale"
    assert source["last_error"] == "ambiguous year"
