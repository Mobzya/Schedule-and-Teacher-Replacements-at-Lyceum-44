import io
from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.config import Settings
from app.db import Database
from app.domain import ParsedLesson, SourceType
from app.services.sync import SyncService


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
    legacy = [source for source in sources if source["name"] == "legacy"]
    assert len(official) == 1 and official[0]["enabled"]
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
