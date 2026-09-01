import io
from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.config import Settings
from app.db import Database
from app.domain import SourceType
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
