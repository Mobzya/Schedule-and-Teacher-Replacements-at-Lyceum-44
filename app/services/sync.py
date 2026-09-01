from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date

import aiohttp

from ..config import Settings
from ..db import Database
from ..domain import SourceType
from ..parsers import parse_date_schedule, parse_replacements, parse_weekly_schedule
from ..source_urls import google_sheet_export_url

logger = logging.getLogger(__name__)


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

    def _validate_year(self, declared: str | None, expected: str | None) -> None:
        if declared and expected and declared != expected:
            raise ValueError(f"В таблице указан учебный год {declared}, ожидается {expected}")

    async def preview(
        self, source_type: SourceType, url: str, forced_date: date | None = None, expected_year: str | None = None
    ) -> dict:
        text = await self.fetch_text(url)
        digest = hashlib.sha256(text.encode()).hexdigest()
        if source_type == SourceType.WEEKLY:
            lessons, declared = parse_weekly_schedule(text)
            self._validate_year(declared, expected_year)
            return {
                "lessons": lessons,
                "replacements": [],
                "declared_year": declared,
                "hash": digest,
                "count": len(lessons),
            }
        if source_type == SourceType.DATE:
            lessons, declared, parsed_date = parse_date_schedule(text, forced_date)
            self._validate_year(declared, expected_year)
            return {
                "lessons": lessons,
                "replacements": [],
                "declared_year": declared,
                "hash": digest,
                "count": len(lessons),
                "date": parsed_date,
            }
        fallback_year = int(self.settings.academic_year.split("-")[0])
        replacements, declared = parse_replacements(text, fallback_year=fallback_year)
        self._validate_year(declared, expected_year)
        return {
            "lessons": [],
            "replacements": replacements,
            "declared_year": declared,
            "hash": digest,
            "count": len(replacements),
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
