from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import re
from datetime import date, datetime, timedelta

try:
    import aiohttp
except ImportError:  # Cloudflare Workers use the runtime fetch() API instead.
    aiohttp = None
from openpyxl import load_workbook

from ..config import Settings
from ..db import Database
from ..domain import SourceType
from ..parsers import explicit_date_academic_years, parse_date_schedule, parse_replacements, parse_weekly_schedule
from ..source_urls import (
    google_sheet_export_url,
    google_sheet_parts,
    google_sheet_public_tabs,
    google_workbook_export_url,
)
from ..utils import academic_year_for, now_local

logger = logging.getLogger(__name__)

OFFICIAL_SCHEDULE_ID = "1yFbLC3cXH_Xuci_VBvxGKt9n6_QZpmTg"
OFFICIAL_SCHEDULE_TABS = (
    ("5", 925792215),
    ("6", 1333352466),
    ("7", 1367582746),
    ("8", 822267835),
    ("9", 1145447977),
    ("10", 1534986807),
    ("11", 551024178),
)
OFFICIAL_SCHEDULE_URL = f"https://docs.google.com/spreadsheets/d/{OFFICIAL_SCHEDULE_ID}/edit"
OFFICIAL_REPLACEMENTS_NAME = "official-replacements-2026-11"
OFFICIAL_REPLACEMENTS_ID = "1bZlIyg9_0JYLnN2WTD3SMxg4zHWe-PaqWDGYnZV1hEg"
OFFICIAL_REPLACEMENTS_URL = f"https://docs.google.com/spreadsheets/d/{OFFICIAL_REPLACEMENTS_ID}/edit?gid=1397336121"
REPLACEMENT_MONTHS = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)


class SyncService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self._lock = asyncio.Lock()

    async def bootstrap(self) -> None:
        revision = hashlib.sha256(
            json.dumps(
                [
                    "monthly-tabs-v1",
                    self.settings.academic_year,
                    self.settings.bootstrap_schedule_sources,
                    self.settings.bootstrap_replacement_sources,
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if await self.db.get_state("sources_bootstrap_revision") == revision:
            return
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
        if self.settings.academic_year == "2026-2027":
            # Process each current grade tab as a small CSV. Exporting/parsing the
            # whole XLSX exceeds the 10 ms CPU budget on Cloudflare Workers Free.
            for grade, gid in OFFICIAL_SCHEDULE_TABS:
                await self.db.bootstrap_source(
                    f"official-2026-2027-grade-{grade}",
                    SourceType.WEEKLY,
                    f"{OFFICIAL_SCHEDULE_URL}?gid={gid}",
                    self.settings.academic_year,
                    priority=200,
                )
            # Retire the former whole-workbook source if it was created by an
            # earlier Worker version; its large XLSX export cannot run for free.
            for source in sources:
                if (
                    source["source_type"] == SourceType.WEEKLY.value
                    and OFFICIAL_SCHEDULE_ID in source["url"]
                    and "gid=" not in source["url"]
                    and source["enabled"]
                ):
                    await self.db.set_source_enabled(int(source["id"]), False)
            replacements_exist = any(
                OFFICIAL_REPLACEMENTS_URL in source["url"] and source["source_type"] == SourceType.REPLACEMENTS.value
                for source in sources
            )
        else:
            replacements_exist = any(
                OFFICIAL_REPLACEMENTS_URL in source["url"] and source["source_type"] == SourceType.REPLACEMENTS.value
                for source in sources
            )
        if self.settings.academic_year == "2026-2027" and not replacements_exist:
            await self.db.bootstrap_source(
                OFFICIAL_REPLACEMENTS_NAME,
                SourceType.REPLACEMENTS,
                OFFICIAL_REPLACEMENTS_URL,
                self.settings.academic_year,
                priority=200,
            )
        await self.db.set_state("sources_bootstrap_revision", revision)

    async def discover_replacement_sources(self, sources=None) -> bool:
        """One small discovery job, separate from CSV parsing on Workers Free.

        Existing monthly snapshots/disabled flags and manually pinned URLs stay
        intact. HTML changes or network failures never erase imported lessons.
        """
        sources = sources if sources is not None else await self.db.list_sources()
        if not any(OFFICIAL_REPLACEMENTS_ID in s["url"] for s in sources):
            return False
        now = now_local(self.settings.timezone)
        retry = await self.db.get_state("replacement_discovery_at")
        try:
            if retry and now < datetime.fromisoformat(retry):
                return False
        except (TypeError, ValueError):
            pass
        # Reserve the retry before reading/parsing: a hard Worker CPU timeout
        # cannot be caught and must not make discovery starve every CSV import.
        await self.db.set_state("replacement_discovery_at", (now + timedelta(minutes=15)).isoformat())
        await self.db.set_state("replacement_discovery_error", "pending")
        try:
            data = await self._fetch_bytes(
                f"https://docs.google.com/spreadsheets/d/{OFFICIAL_REPLACEMENTS_ID}/htmlview",
                30,
            )
            if len(data) > 2_000_000:
                raise ValueError("Список вкладок слишком большой")
            tabs = google_sheet_public_tabs(data.decode("utf-8-sig"))
            candidates = []
            for title, gid in tabs:
                match = re.fullmatch(r"замен[аы]\s+([а-яё]+)\s+(20\d{2})", title.strip(), re.I)
                if not match or match[1].lower() not in REPLACEMENT_MONTHS:
                    continue
                month = date(int(match[2]), REPLACEMENT_MONTHS.index(match[1].lower()) + 1, 1)
                if academic_year_for(month) == self.settings.academic_year:
                    candidates.append((month, gid))
            if not candidates:
                raise ValueError("Не найдены месячные вкладки замен текущего учебного года")
            known = set()
            for source in sources:
                if source["source_type"] == SourceType.REPLACEMENTS.value:
                    try:
                        known.add(google_sheet_parts(source["url"]))
                    except ValueError:
                        pass
            for month, gid in candidates:
                if (OFFICIAL_REPLACEMENTS_ID, gid) in known:
                    continue
                await self.db.bootstrap_source(
                    f"official-replacements-{month:%Y-%m}",
                    SourceType.REPLACEMENTS,
                    f"https://docs.google.com/spreadsheets/d/{OFFICIAL_REPLACEMENTS_ID}/edit?gid={gid}",
                    self.settings.academic_year,
                    priority=200,
                )
                known.add((OFFICIAL_REPLACEMENTS_ID, gid))
        except Exception as exc:
            # A broken tab list must not monopolize the sync cursor. Retry later;
            # previously discovered sources keep updating independently.
            await self.db.set_state("replacement_discovery_at", (now + timedelta(minutes=15)).isoformat())
            await self.db.set_state("replacement_discovery_error", type(exc).__name__)
            logger.warning("Replacement tab discovery failed: %s", type(exc).__name__)
        else:
            await self.db.set_state("replacement_discovery_at", (now + timedelta(hours=6)).isoformat())
            await self.db.set_state("replacement_discovery_error", "")
        return True

    async def fetch_text(self, url: str) -> str:
        export_url = google_sheet_export_url(url)
        data = await self._fetch_bytes(export_url, 30)
        if len(data) > 5_000_000:
            raise ValueError("Источник слишком большой (>5 MB)")
        return data.decode("utf-8-sig", errors="replace")

    async def fetch_sheets(self, url: str) -> list[tuple[str, str]]:
        """Fetch one explicitly selected sheet or every visible sheet in a workbook."""
        _, gid = google_sheet_parts(url)
        if gid is not None:
            return [(f"gid={gid}", await self.fetch_text(url))]

        data = await self._fetch_bytes(google_workbook_export_url(url), 60)
        if len(data) > 15_000_000:
            raise ValueError("Файл Google Sheets слишком большой (>15 MB)")
        return self._xlsx_sheets(data)

    @staticmethod
    async def _fetch_bytes(url: str, timeout_seconds: int) -> bytes:
        if aiohttp is not None:
            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": "Lyceum44ScheduleBot/3"},
                trust_env=True,
            ) as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    return await response.read()

        # Cloudflare Workers exposes outbound HTTP through JavaScript fetch().
        from js import AbortSignal, Object, Uint8Array, fetch
        from pyodide.ffi import to_js

        response = await fetch(
            url,
            to_js(
                {
                    "headers": {"User-Agent": "Lyceum44ScheduleBot/3"},
                    "signal": AbortSignal.timeout(timeout_seconds * 1000),
                },
                dict_converter=Object.fromEntries,
            ),
        )
        if not response.ok:
            raise ValueError(f"Источник ответил HTTP {response.status}")
        buffer = await response.arrayBuffer()
        return bytes(Uint8Array.new(buffer).to_py())

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
                        date_years = explicit_date_academic_years(text)
                        if date_years == {expected_year}:
                            declared = expected_year
                        else:
                            found = ", ".join(sorted(date_years)) or "не найден"
                            raise ValueError(
                                "В таблице замен не указан учебный год, а полные даты "
                                f"относятся к: {found}; ожидается {expected_year}"
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
            await self.discover_replacement_sources()
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

    async def sync_next(self) -> dict[str, int]:
        """Advance one enabled source per invocation, persisting the cursor before work.

        The source is retried on a later pass if the download or import fails. Advancing
        first means one bad spreadsheet cannot permanently starve later sources.
        """
        sources = await self.db.list_sources(enabled_only=True)
        if not sources:
            return {"ok": 0, "failed": 0, "skipped": 1}
        if await self.discover_replacement_sources(await self.db.list_sources()):
            return {"ok": 0, "failed": 0, "skipped": 1}
        # list_sources groups by type/priority, not by ID. The cursor is an ID, so
        # using the database order here can pin the cursor to its first item and
        # starve later sources indefinitely.
        sources.sort(key=lambda item: int(item["id"]))
        cursor = int(await self.db.get_state("source_sync_cursor", "0") or 0)
        current = now_local(self.settings.timezone)
        preferred = [
            s
            for s in sources
            if s["source_type"] == SourceType.REPLACEMENTS.value
            and s["name"] == f"official-replacements-{current:%Y-%m}"
        ]
        priority_turn = await self.db.get_state("source_sync_priority_turn", "0")
        if preferred and priority_turn == "0":
            source = preferred[0]
            await self.db.set_state("source_sync_priority_turn", "1")
        else:
            ordinary = [s for s in sources if s not in preferred] or sources
            source = next((item for item in ordinary if int(item["id"]) > cursor), ordinary[0])
            await self.db.set_state("source_sync_priority_turn", "0")
            await self.db.set_state("source_sync_cursor", str(source["id"]))
        source_id = int(source["id"])
        try:
            await self.sync_source(source_id)
        except Exception:
            logger.exception("Source sync failed; it will be retried on the next pass: %s", source["name"])
            return {"ok": 0, "failed": 1, "skipped": 0}
        return {"ok": 1, "failed": 0, "skipped": 0}
