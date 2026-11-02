import io
from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.config import Settings
from app.db import Database
from app.domain import ParsedLesson, ParsedReplacement, PublicationState, SourceType
from app.services.calendar import CalendarService
from app.services.schedule import ScheduleService
from app.services.sync import (
    LEGACY_REPLACEMENTS_URL,
    OFFICIAL_REPLACEMENTS_ID,
    OFFICIAL_REPLACEMENTS_NAME,
    OFFICIAL_REPLACEMENTS_URL,
    SyncService,
)
from app.source_urls import google_sheet_parts


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

    official = [source for source in sources if source["name"] == "official-2026-2027"]
    replacements = [source for source in sources if OFFICIAL_REPLACEMENTS_ID in source["url"]]
    legacy = [source for source in sources if source["name"] == "legacy"]
    assert len(official) == 1 and official[0]["enabled"]
    assert len(replacements) == 1 and replacements[0]["enabled"]
    assert google_sheet_parts(replacements[0]["url"])[1] is None
    assert len(legacy) == 1 and not legacy[0]["enabled"]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_bootstrap_migrates_managed_september_link_without_losing_snapshot(tmp_path: Path, enabled):
    path = tmp_path / "db.sqlite"
    db = Database(path)
    await db.init()
    sid = await db.add_source(
        OFFICIAL_REPLACEMENTS_NAME, SourceType.REPLACEMENTS, LEGACY_REPLACEMENTS_URL, 200, None, None, "2026-2027", None
    )
    await db.replace_source_snapshot(
        sid, SourceType.REPLACEMENTS, [], [ParsedReplacement(date(2026, 9, 30), "11а", 2, "Информатика", None, "301")]
    )
    await db.source_status(sid, "ok", None, "september-hash", True)
    await db.set_source_enabled(sid, enabled)
    service = SyncService(db, _settings(path))

    await service.bootstrap()
    source = await db.get_source(sid)

    assert source["url"] == OFFICIAL_REPLACEMENTS_URL
    assert bool(source["enabled"]) is enabled
    assert source["priority"] == 200
    assert source["content_hash"] is None
    assert source["last_status"] == "never"
    async with db.connect() as connection:
        rows = await (await connection.execute("SELECT * FROM replacements WHERE source_id=?", (sid,))).fetchall()
    assert len(rows) == 1 and rows[0]["lesson_date"] == "2026-09-30"

    await db.source_status(sid, "ok", None, "november-hash", True)
    await service.bootstrap()
    assert (await db.get_source(sid))["content_hash"] == "november-hash"
    assert len([s for s in await db.list_sources() if OFFICIAL_REPLACEMENTS_ID in s["url"]]) == 1


@pytest.mark.asyncio
async def test_bootstrap_preserves_manual_single_tab_link(tmp_path: Path):
    path = tmp_path / "db.sqlite"
    db = Database(path)
    await db.init()
    sid = await db.add_source(
        "September archive", SourceType.REPLACEMENTS, LEGACY_REPLACEMENTS_URL, 100, None, None, "2026-2027", 1
    )

    await SyncService(db, _settings(path)).bootstrap()

    assert (await db.get_source(sid))["url"] == LEGACY_REPLACEMENTS_URL


@pytest.mark.asyncio
async def test_new_month_workbook_replacements_reach_schedule_and_preserve_snapshot_on_error(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "db.sqlite"
    db = Database(path)
    await db.init()
    service = SyncService(db, _settings(path))
    await service.bootstrap()
    source = next(s for s in await db.list_sources() if s["name"] == OFFICIAL_REPLACEMENTS_NAME)
    workbook = Workbook()
    november = workbook.active
    november.title = "Замена ноябрь 2026"
    november.append(
        ["дата", "урок", "Класс", "Предмет", "Отсутствующий учитель", "Основание", "Заменяющий учитель", "Кабинет"]
    )
    november.append(["02.11.2026"])
    november.append(["02.ноя", 4, "11а", "Информатика", "Вакансия", "объединение", "Мулюкова А.В.", 301])
    september = workbook.create_sheet("Замена сентябрь")
    september.sheet_state = "hidden"
    september.append(["дата", "урок", "Класс", "Предмет"])
    september.append(["30.09.2026", 4, "11а", "История"])
    workbook.create_sheet("Служебный лист").append(["2026-2027 учебный год"])
    output = io.BytesIO()
    workbook.save(output)

    async def fetch_workbook(url):
        assert google_sheet_parts(url)[1] is None
        return service._xlsx_sheets(output.getvalue())

    monkeypatch.setattr(service, "fetch_sheets", fetch_workbook)
    result = await service.sync_source(source["id"])
    await db.set_publication_state(PublicationState.READY)
    schedules = ScheduleService(db, _settings(path), CalendarService(db))

    assert result["count"] == 1
    info = await schedules.day_schedule("11а", None, date(2026, 11, 2))
    lesson = info["lessons"][0]
    assert info["state"] == "ready"
    assert lesson.changed and lesson.subject == "Информатика"
    assert lesson.teacher == "Мулюкова А.В." and lesson.room == "301"
    assert await db.replacement_summary() == {"count": 1, "first_date": "2026-11-02", "last_date": "2026-11-02"}

    async def unavailable(_url):
        raise ValueError("Google Sheets временно недоступен")

    monkeypatch.setattr(service, "fetch_sheets", unavailable)
    with pytest.raises(ValueError, match="временно недоступен"):
        await service.sync_source(source["id"])
    assert (await db.get_source(source["id"]))["last_status"] == "error"
    assert (await schedules.day_schedule("11а", None, date(2026, 11, 2)))["lessons"] == info["lessons"]


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
