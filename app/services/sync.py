from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
from datetime import date, datetime

import aiohttp
from openpyxl import load_workbook

from ..config import Settings
from ..db import Database
from ..domain import SourceType
from ..parsers import parse_date_schedule, parse_replacements, parse_weekly_schedule
from ..source_urls import google_sheet_export_url, google_sheet_parts, google_workbook_export_url

logger = logging.getLogger(__name__)

OFFICIAL_SCHEDULE_NAME = "official-2026-2027"
OFFICIAL_SCHEDULE_ID = "1yFbLC3cXH_Xuci_VBvxGKt9n6_QZpmTg"
OFFICIAL_SCHEDULE_URL = f"https://docs.google.com/spreadsheets/d/{OFFICIAL_SCHEDULE_ID}/edit?usp=drivesdk"


class SyncService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self._lock = asyncio.Lock()

    async def bootstrap(self) -> None:
        for name, url in self.settings.bootstrap_schedule_sources.items():
            await self.db.bootstrap_source(name, SourceType.WEEKLY, url, self.settings.academic_year)
        for name, url in self.settings.bootstrap_replacement_sources.items():
            await self.db.bootstrap_source(name, SourceType.REPLACEMENTS, url, self.settings.academic_year)
        await self.db.disable_sources_by_url_fragment(
            "1ssv52YzwfZ3S-azlDu49_hyLnF-TMJYf3lo-WbdmvI8",
            "Старая таблица замен без указания учебного года отключена",
        )
        await self.db.disable_sources_by_url_fragment(
            "1u117ewxXm5KavaFK2y2UFSOV8Qn5AejrXzNeUG3aIOs",
            "Источник прошлого учебного года отключён после публикации итогового расписания",
        )
        sources = await self.db.list_sources()
        official_exists = any(
            OFFICIAL_SCHEDULE_ID in source["url"] and source["source_type"] == SourceType.WEEKLY.value
            for source in sources
        )
        if self.settings.academic_year == "2026-2027" and not official_exists:
            await self.db.bootstrap_source(
                OFFICIAL_SCHEDULE_NAME,
                SourceType.WEEKLY,
                OFFICIAL_SCHEDULE_URL,
                self.settings.academic_year,
                priority=200,
            )

    async def fetch_text(self, url: str) -> str:
        export_url = google_sheet_export_url(url)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "Lyceum44ScheduleBot/3"}) as session:
            async with session.get(export_url) as response:
                response.raise_for_status()
                data = await response.read()
        if len(data) > 5_000_000:
            raise ValueError("Источник слишком большой (>5 MB)")
        return data.decode("utf-8-sig", errors="replace")

    async def fetch_sheets(self, url: str) -> list[tuple[str, str]]:
        """Fetch one explicitly selected sheet or every visible sheet in a workbook."""
        _, gid = google_sheet_parts(url)
        if gid is not None:
            return [(f"gid={gid}", await self.fetch_text(url))]

        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "Lyceum44ScheduleBot/3"}) as session:
            async with session.get(google_workbook_export_url(url)) as response:
                response.raise_for_status()
                data = await response.read()
        if len(data) > 15_000_000:
            raise ValueError("Файл Google Sheets слишком большой (>15 MB)")
        return await asyncio.to_thread(self._xlsx_sheets, data)

    @staticmethod
    def _xlsx_sheets(data: bytes) -> list[tuple[str, str]]:
        try:
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:
            raise ValueError("Не удалось прочитать книгу Google Sheets") from exc
        result = []
        try:
            for sheet in workbook.worksheets:
                if sheet.sheet_state != "visible":
                    continue
                output = io.StringIO(newline="")
                writer = csv.writer(output)
                for row in sheet.iter_rows(values_only=True):
                    values = []
                    for value in row:
                        if isinstance(value, date | datetime):
                            value = value.strftime("%d.%m.%Y")
                        elif isinstance(value, float) and value.is_integer():
                            value = int(value)
                        values.append("" if value is None else value)
                    writer.writerow(values)
                result.append((sheet.title, output.getvalue()))
        finally:
            workbook.close()
        return result

    def _validate_year(self, declared: str | None, expected: str | None) -> None:
        if declared and expected and declared != expected:
            raise ValueError(f"В таблице указан учебный год {declared}, ожидается {expected}")

    async def preview(
        self, source_type: SourceType, url: str, forced_date: date | None = None, expected_year: str | None = None
    ) -> dict:
        sheets = await self.fetch_sheets(url)
        digest = hashlib.sha256("\n".join(f"{name}\n{text}" for name, text in sheets).encode()).hexdigest()
        lessons = []
        replacements = []
        declared_years = set()
        parsed_date = forced_date
        rejected = []
        accepted = 0
        fallback_year = int(self.settings.academic_year.split("-")[0])

        for name, text in sheets:
            try:
                if source_type == SourceType.WEEKLY:
                    parsed, declared = parse_weekly_schedule(text)
                    sheet_date = None
                elif source_type == SourceType.DATE:
                    parsed, declared, sheet_date = parse_date_schedule(text, forced_date)
                else:
                    parsed, declared = parse_replacements(text, fallback_year=fallback_year)
                    sheet_date = None
                    if expected_year and not declared:
                        raise ValueError(
                            f"В таблице замен не указан учебный год; добавьте строку «{expected_year} учебный год»"
                        )
                self._validate_year(declared, expected_year)
            except ValueError as exc:
                rejected.append(f"{name}: {exc}")
                continue
            if declared:
                declared_years.add(declared)
            accepted += 1
            if source_type == SourceType.REPLACEMENTS:
                replacements.extend(parsed)
            else:
                lessons.extend(parsed)
                parsed_date = parsed_date or sheet_date

        if not accepted:
            year_error = next((x.split(": ", 1)[1] for x in rejected if "В таблице указан учебный год" in x), None)
            if year_error:
                raise ValueError(year_error)
            details = "; ".join(rejected[:4])
            raise ValueError(f"Ни одна вкладка не подошла. {details}".strip())

        if source_type == SourceType.REPLACEMENTS:
            replacements = list({(x.lesson_date, x.class_name, x.lesson_number): x for x in replacements}.values())
        else:
            lessons = list(
                {(x.class_name, x.profile, x.weekday, x.lesson_date, x.lesson_number): x for x in lessons}.values()
            )
        declared = expected_year if expected_year in declared_years else next(iter(declared_years), None)
        return {
            "lessons": lessons,
            "replacements": replacements,
            "declared_year": declared,
            "hash": digest,
            "count": len(replacements) if source_type == SourceType.REPLACEMENTS else len(lessons),
            "classes": sorted({x.class_name for x in (*lessons, *replacements)}),
            **({"date": parsed_date} if source_type == SourceType.DATE else {}),
        }

    async def sync_source(self, source_id: int) -> dict:
        source = await self.db.get_source(source_id)
        if not source:
            raise ValueError("Источник не найден")
        source_type = SourceType(source["source_type"])
        forced_date = (
            date.fromisoformat(source["valid_from"])
            if source_type == SourceType.DATE and source["valid_from"]
            else None
        )
        try:
            preview = await self.preview(source_type, source["url"], forced_date, source["expected_academic_year"])
            # Пустой snapshot расписания считается ошибкой. Для замен пустой лист допустим,
            # но только если он относится к ожидаемому году или не декларирует год вообще.
            if source_type in (SourceType.WEEKLY, SourceType.DATE) and preview["count"] == 0:
                raise ValueError("Источник не содержит расписания")
            if source["last_status"] == "ok" and source["content_hash"] == preview["hash"]:
                await self.db.source_status(source_id, "ok", None, preview["hash"], True)
                preview["unchanged"] = True
                return preview
            await self.db.replace_source_snapshot(source_id, source_type, preview["lessons"], preview["replacements"])
            await self.db.source_status(source_id, "ok", None, preview["hash"], True)
            return preview
        except Exception as exc:
            status = "stale" if str(exc).startswith("В таблице указан учебный год") else "error"
            await self.db.source_status(source_id, status, str(exc))
            raise

    async def sync_all(self) -> dict[str, int]:
        if self._lock.locked():
            return {"ok": 0, "failed": 0, "skipped": 1}
        async with self._lock:
            ok = failed = skipped = 0
            for source in await self.db.list_sources(enabled_only=True):
                try:
                    await self.sync_source(int(source["id"]))
                    ok += 1
                except Exception:
                    updated = await self.db.get_source(int(source["id"]))
                    if updated and updated["last_status"] == "stale":
                        logger.info("Source is for another academic year, skipped: %s", source["name"])
                        skipped += 1
                    else:
                        logger.exception("Source sync failed: %s", source["name"])
                        failed += 1
            return {"ok": ok, "failed": failed, "skipped": skipped}
