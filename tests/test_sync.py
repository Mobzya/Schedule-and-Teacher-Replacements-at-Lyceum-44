import io
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from openpyxl import Workbook

from app.config import Settings
from app.db import Database
from app.domain import ParsedLesson, ParsedReplacement, SourceType
from app.services.sync import (
    OFFICIAL_REPLACEMENTS_ID,
    OFFICIAL_REPLACEMENTS_URL,
    OFFICIAL_SCHEDULE_ID,
    OFFICIAL_SCHEDULE_TABS,
    SyncService,
)


def test_xlsx_sheets_reads_visible_tabs_and_integral_numbers():
    workbook = Workbook()
    visible = workbook.active
    visible.title = "5 классы"
    visible.append(["2026-2027 учебный год", 1.0, 212.0])
    hidden = workbook.create_sheet("старое")
    hidden.sheet_state = "hidden"
    hidden.append(["2020-2021 учебный год"])
    output = io.BytesIO()
    workbook.save(output)

    sheets = SyncService._xlsx_sheets(output.getvalue())

    assert sheets == [("5 классы", "2026-2027 учебный год,1,212\r\n")]


def _settings(path: Path) -> Settings:
    return Settings("", frozenset(), None, "Europe/Moscow", "2026-2027", path, 10000, 10, 30, 15, {}, {}, {}, {})


@pytest.mark.asyncio
async def test_multisheet_preview_combines_current_year_and_skips_old(tmp_path: Path, monkeypatch):
    service = SyncService(Database(tmp_path / "db.sqlite"), _settings(tmp_path / "db.sqlite"))
    current = """2026-2027 учебный год\n,,5а,каб\nсреда,1,Алгебра,101\n,2,Русский,102\n,3,История,103\n"""
    old = current.replace("2026-2027", "2025-2026").replace("5а", "6а")

    async def fake_fetch(_url):
        return [("актуальное", current), ("старое", old)]

    monkeypatch.setattr(service, "fetch_sheets", fake_fetch)
    result = await service.preview(SourceType.DATE, "unused", date(2026, 9, 2), "2026-2027")

    assert result["classes"] == ["5а"]
    assert result["count"] == 3
    assert result["declared_year"] == "2026-2027"


@pytest.mark.asyncio
async def test_replacements_without_explicit_year_are_rejected(tmp_path: Path, monkeypatch):
    service = SyncService(Database(tmp_path / "db.sqlite"), _settings(tmp_path / "db.sqlite"))
    replacements = """дата,урок,класс,предмет\n02.09,1,5а,Математика\n"""

    async def fake_fetch(_url):
        return [("замены", replacements)]

    monkeypatch.setattr(service, "fetch_sheets", fake_fetch)
    with pytest.raises(ValueError, match="не указан учебный год"):
        await service.preview(SourceType.REPLACEMENTS, "unused", None, "2026-2027")


@pytest.mark.asyncio
async def test_replacements_year_can_be_proven_by_full_date_sections(tmp_path: Path, monkeypatch):
    service = SyncService(Database(tmp_path / "db.sqlite"), _settings(tmp_path / "db.sqlite"))
    replacements = """дата,урок,класс,предмет
14.09.2026 понедельник,,,
14.сен,1,5а,Математика
"""

    async def fake_fetch(_url):
        return [("замены", replacements)]

    monkeypatch.setattr(service, "fetch_sheets", fake_fetch)
    result = await service.preview(SourceType.REPLACEMENTS, "unused", None, "2026-2027")

    assert result["count"] == 1
    assert result["declared_year"] == "2026-2027"
    assert result["replacements"][0].lesson_date == date(2026, 9, 14)


@pytest.mark.asyncio
async def test_bootstrap_adds_official_schedule_and_retires_legacy(tmp_path: Path):
    path = tmp_path / "db.sqlite"
    base = _settings(path)
    settings = Settings(
        base.bot_token,
        base.admin_ids,
        base.database_url,
        base.timezone_name,
        base.academic_year,
        base.database_path,
        base.port,
        base.source_sync_minutes,
        base.notification_tick_seconds,
        base.default_reminder_minutes,
        {"legacy": "https://docs.google.com/spreadsheets/d/1u117ewxXm5KavaFK2y2UFSOV8Qn5AejrXzNeUG3aIOs/edit"},
        {},
        base.bell_schedules,
        base.class_bell_schedules,
    )
    db = Database(path)
    await db.init()

    await SyncService(db, settings).bootstrap()
    sources = await db.list_sources()

    official = [source for source in sources if OFFICIAL_SCHEDULE_ID in source["url"] and "gid=" in source["url"]]
    replacements = [source for source in sources if source["url"] == OFFICIAL_REPLACEMENTS_URL]
    legacy = [source for source in sources if source["name"] == "legacy"]
    assert len(official) == len(OFFICIAL_SCHEDULE_TABS) and all(source["enabled"] for source in official)
    assert len(replacements) == 1 and replacements[0]["enabled"]
    assert len(legacy) == 1 and not legacy[0]["enabled"]


@pytest.mark.asyncio
async def test_unchanged_source_does_not_rewrite_snapshot(tmp_path: Path, monkeypatch):
    path = tmp_path / "db.sqlite"
    db = Database(path)
    await db.init()
    source_id = await db.add_source("weekly", SourceType.WEEKLY, "unused", 100, None, None, "2026-2027", 1)
    lesson = ParsedLesson("10а", None, 0, None, 1, "Алгебра", "313")
    await db.replace_source_snapshot(source_id, SourceType.WEEKLY, [lesson], [])
    await db.source_status(source_id, "ok", None, "same-hash", True)
    service = SyncService(db, _settings(path))

    async def fake_preview(*_args):
        return {
            "lessons": [lesson],
            "replacements": [],
            "declared_year": "2026-2027",
            "hash": "same-hash",
            "count": 1,
        }

    async def fail_rewrite(*_args):
        raise AssertionError("unchanged snapshot was rewritten")

    monkeypatch.setattr(service, "preview", fake_preview)
    monkeypatch.setattr(db, "replace_source_snapshot", fail_rewrite)

    result = await service.sync_source(source_id)
    assert result["unchanged"] is True


@pytest.mark.asyncio
async def test_sync_next_round_robins_sources_even_when_db_order_is_by_type(tmp_path: Path, monkeypatch):
    path = tmp_path / "db.sqlite"
    db = Database(path)
    await db.init()
    weekly_id = await db.add_source("weekly", SourceType.WEEKLY, "unused-weekly", 200, None, None, "2026-2027", 1)
    replacements_id = await db.add_source(
        "replacements", SourceType.REPLACEMENTS, "unused-replacements", 200, None, None, "2026-2027", 1
    )
    service = SyncService(db, _settings(path))
    synced = []

    async def fake_sync(source_id):
        synced.append(source_id)
        return {"ok": True}

    monkeypatch.setattr(service, "sync_source", fake_sync)

    await service.sync_next()
    await service.sync_next()

    assert synced == [weekly_id, replacements_id]


TAB_LIST = """<script>
items.push({name: "Замена ноябрь 2026", pageUrl: "https://untrusted.example/", gid: "1397336121",initialSheet: true});
items.push({name: "Замена декабрь 2026", pageUrl: "ignored", gid: "12345",initialSheet: false});
items.push({name: "Замена январь 2027", pageUrl: "ignored", gid: "12346",initialSheet: false});
items.push({name: "Замена ноябрь 2025", pageUrl: "ignored", gid: "12347",initialSheet: false});
items.push({name: "Замена ноябрь 2026 (копия)", pageUrl: "ignored", gid: "12348",initialSheet: false});
</script>"""


@pytest.mark.asyncio
async def test_discovers_new_months_without_xlsx_and_preserves_disabled_tabs(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    db = Database(tmp_path / "discovery.sqlite")
    await db.init()
    service = SyncService(db, _settings(tmp_path / "discovery.sqlite"))
    await service.bootstrap()
    november = next(s for s in await db.list_sources() if s["url"] == OFFICIAL_REPLACEMENTS_URL)
    await db.set_source_enabled(november["id"], False)
    fetch = AsyncMock(return_value=TAB_LIST.encode())
    monkeypatch.setattr(service, "_fetch_bytes", fetch)
    monkeypatch.setattr(
        "app.services.sync.now_local", lambda _: datetime(2026, 11, 2, tzinfo=ZoneInfo("Europe/Moscow"))
    )
    assert await service.discover_replacement_sources()
    sources = [s for s in await db.list_sources() if OFFICIAL_REPLACEMENTS_ID in s["url"]]
    assert len(sources) == 3
    assert {s["name"] for s in sources} == {
        "official-replacements-2026-11",
        "official-replacements-2026-12",
        "official-replacements-2027-01",
    }
    assert not (await db.get_source(november["id"]))["enabled"]
    assert all(s["url"].startswith("https://docs.google.com/") for s in sources)
    assert not await service.discover_replacement_sources()
    fetch.assert_awaited_once()
    assert "/htmlview" in fetch.await_args.args[0]


@pytest.mark.asyncio
async def test_failed_discovery_does_not_block_imports_or_erase_snapshot(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    db = Database(tmp_path / "failed-discovery.sqlite")
    await db.init()
    service = SyncService(db, _settings(tmp_path / "failed-discovery.sqlite"))
    await service.bootstrap()
    source = next(s for s in await db.list_sources() if s["url"] == OFFICIAL_REPLACEMENTS_URL)
    await db.replace_source_snapshot(
        source["id"],
        SourceType.REPLACEMENTS,
        [],
        [
            ParsedReplacement(date(2026, 11, 2), "11а", 4, "Физика", None, "301"),
        ],
    )
    monkeypatch.setattr(service, "_fetch_bytes", AsyncMock(return_value=b"<html>sign in</html>"))
    sync = AsyncMock()
    monkeypatch.setattr(service, "sync_source", sync)
    assert (await service.sync_next())["skipped"] == 1
    assert await db.get_state("replacement_discovery_error") == "ValueError"
    assert (await service.sync_next())["ok"] == 1
    sync.assert_awaited_once()
    assert (await db.replacement_summary())["count"] == 1


@pytest.mark.asyncio
async def test_current_replacements_are_prioritized_without_starving_weekly(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    db = Database(tmp_path / "priority.sqlite")
    await db.init()
    service = SyncService(db, _settings(tmp_path / "priority.sqlite"))
    weekly = await db.add_source("weekly", SourceType.WEEKLY, "unused", 200, None, None, "2026-2027", None)
    another = await db.add_source("weekly2", SourceType.WEEKLY, "unused2", 200, None, None, "2026-2027", None)
    current = await db.add_source(
        "official-replacements-2026-11",
        SourceType.REPLACEMENTS,
        OFFICIAL_REPLACEMENTS_URL,
        200,
        None,
        None,
        "2026-2027",
        None,
    )
    monkeypatch.setattr(
        "app.services.sync.now_local", lambda _: datetime(2026, 11, 2, tzinfo=ZoneInfo("Europe/Moscow"))
    )
    monkeypatch.setattr(service, "discover_replacement_sources", AsyncMock(return_value=False))
    sync = AsyncMock()
    monkeypatch.setattr(service, "sync_source", sync)
    for _ in range(6):
        await service.sync_next()
    assert [call.args[0] for call in sync.await_args_list] == [current, weekly, current, another, current, weekly]


@pytest.mark.asyncio
async def test_bootstrap_is_cached_and_does_not_consume_source_ids(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    db = Database(tmp_path / "bootstrap.sqlite")
    await db.init()
    service = SyncService(db, _settings(tmp_path / "bootstrap.sqlite"))
    await service.bootstrap()
    bootstrap = AsyncMock(wraps=db.bootstrap_source)
    monkeypatch.setattr(db, "bootstrap_source", bootstrap)
    await service.bootstrap()
    bootstrap.assert_not_awaited()
    async with db.connect() as connection:
        before = await (
            await connection.execute("SELECT seq FROM sqlite_sequence WHERE name='data_sources'")
        ).fetchone()
    await db.bootstrap_source(
        "official-replacements-2026-11", SourceType.REPLACEMENTS, OFFICIAL_REPLACEMENTS_URL, "2026-2027"
    )
    async with db.connect() as connection:
        after = await (await connection.execute("SELECT seq FROM sqlite_sequence WHERE name='data_sources'")).fetchone()
    assert before["seq"] == after["seq"]
