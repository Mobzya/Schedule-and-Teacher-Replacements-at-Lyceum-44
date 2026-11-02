"""Check public Sheets imports and every class's week in a disposable database.

Run with: uv run --group local python -m scripts.check_sources
This does not connect to production D1 or send Telegram messages.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from datetime import date, timedelta
from pathlib import Path

from app.config import settings_from_mapping
from app.db import Database
from app.domain import PublicationState, SourceType
from app.services.calendar import CalendarService
from app.services.schedule import ScheduleService
from app.services.sync import OFFICIAL_SCHEDULE_TABS, SyncService
from app.ui import render_day
from app.utils import now_local


async def run(replacements_only: bool = False):
    with tempfile.TemporaryDirectory(prefix="lyceum44-source-check-") as directory:
        path = Path(directory) / "check.sqlite"
        settings = settings_from_mapping(
            {"DATABASE_PATH": str(path), "ACADEMIC_YEAR": "2026-2027"}, require_token=False
        )
        db = Database(path)
        await db.init()
        sync = SyncService(db, settings)
        await sync.bootstrap()
        if replacements_only:
            # Diagnostic database only: a closed weekly workbook must not stop
            # an independent end-to-end check of the public replacements.
            for source in await db.list_sources(True):
                if source["source_type"] != SourceType.REPLACEMENTS.value:
                    await db.set_source_enabled(source["id"], False)
        result = await sync.sync_all()
        if result["failed"]:
            errors = [
                f"{source['name']}: {source['last_error']}"
                for source in await db.list_sources(True)
                if source["last_status"] != "ok"
            ]
            raise RuntimeError("; ".join(errors))
        await db.set_publication_state(PublicationState.READY)
        if replacements_only:
            schedules = ScheduleService(db, settings, CalendarService(db))
            async with db.connect() as connection:
                rows = await (
                    await connection.execute(
                        "SELECT DISTINCT class_name,lesson_date FROM replacements ORDER BY lesson_date,class_name"
                    )
                ).fetchall()
                summary = await (
                    await connection.execute(
                        "SELECT lesson_date,COUNT(*) AS count FROM replacements GROUP BY lesson_date ORDER BY lesson_date"
                    )
                ).fetchall()
            if not rows:
                raise RuntimeError("Ни одной замены не импортировано")
            for row in rows:
                day = date.fromisoformat(row["lesson_date"])
                info = await schedules.day_schedule(row["class_name"], None, day)
                if info["state"] not in {"ready", "day_off"}:
                    raise RuntimeError(f"Замена не дошла до расписания: {row['class_name']}, {day}")
                if len(render_day(day, row["class_name"], info)) > 4096:
                    raise RuntimeError("Экран замен превышает лимит Telegram")
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "sources": result["ok"],
                        "replacements_by_date": [dict(row) for row in summary],
                        "checked_class_days": len(rows),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        classes = await db.classes()
        expected_grades = {grade for grade, _ in OFFICIAL_SCHEDULE_TABS}
        found_grades = {"".join(filter(str.isdigit, name)) for name, _ in classes}
        if found_grades != expected_grades:
            raise RuntimeError(f"Не все параллели импортированы: {sorted(found_grades)}")
        schedules = ScheduleService(db, settings, CalendarService(db))
        today = now_local(settings.timezone).date()
        monday = today - timedelta(days=today.weekday())
        checked = 0
        for name, profile in classes:
            for offset in range(7):
                day = monday + timedelta(days=offset)
                info = await schedules.day_schedule(name, profile, day)
                if info["state"] not in {"ready", "day_off"}:
                    raise RuntimeError(f"Нет расписания: {name}, {profile}, {day}: {info['state']}")
                if len(render_day(day, name, info)) > 4096:
                    raise RuntimeError(f"Слишком длинный экран: {name}, {day}")
                checked += 1
        async with db.connect() as connection:
            cursor = await connection.execute("SELECT COUNT(*) AS count FROM weekly_lessons")
            lessons = int((await cursor.fetchone())["count"])
            cursor = await connection.execute("SELECT COUNT(*) AS count FROM replacements")
            replacements = int((await cursor.fetchone())["count"])
        print(
            json.dumps(
                {
                    "status": "ok",
                    "sources": result["ok"],
                    "classes": len({name for name, _ in classes}),
                    "weekly_lessons": lessons,
                    "replacements": replacements,
                    "week_start": monday.isoformat(),
                    "checked_day_views": checked,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replacements-only", action="store_true", help="Не проверять основное недельное расписание")
    asyncio.run(run(parser.parse_args().replacements_only))
