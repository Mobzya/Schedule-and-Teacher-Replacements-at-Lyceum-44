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


@pytest.mark.asyncio
async def test_disabled_source_classes_are_not_offered(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    await db.init()
    active = await db.add_source("active", SourceType.WEEKLY, "x", 100, None, None, "2026-2027", 1)
    disabled = await db.add_source("disabled", SourceType.WEEKLY, "y", 100, None, None, "2026-2027", 1)
    await db.replace_source_snapshot(
        active, SourceType.WEEKLY, [ParsedLesson("10а", None, 0, None, 1, "Алгебра", "1")], []
    )
    await db.replace_source_snapshot(
        disabled, SourceType.WEEKLY, [ParsedLesson("6а", None, 0, None, 1, "Музыка", "2")], []
    )
    await db.set_source_enabled(disabled, False)

    assert await db.classes() == [("10а", None)]


@pytest.mark.asyncio
async def test_switching_to_unprofiled_class_clears_old_profile(tmp_path):
    db = Database(tmp_path / "switch.db")
    await db.init()
    await db.set_user_class(42, "10а", "технологический", "2026-2027", 15)
    await db.set_user_class(42, "5а", None, "2026-2027", 15)
    assert (await db.get_user(42))["profile"] is None
    await db.upsert_user(42, "2026-2027")
    assert (await db.get_user(42))["class_name"] == "5а"


@pytest.mark.asyncio
async def test_unknown_legacy_year_resets_class(tmp_path):
    db = Database(tmp_path / "legacy.db")
    await db.init()
    await db.set_user_class(42, "10а", "технологический", "2026-2027", 15)
    async with db.connect() as connection:
        await connection.execute("UPDATE users SET academic_year=NULL WHERE user_id=42")
        await connection.commit()
    await db.upsert_user(42, "2026-2027")
    assert (await db.get_user(42))["class_name"] is None
    assert (await db.get_user(42))["profile"] is None
