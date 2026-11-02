from datetime import date

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.db import Database
from app.domain import ParsedReplacement, SourceType
from app.health import make_app


@pytest.mark.asyncio
async def test_health_reports_dates_of_active_replacements(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    await db.init()
    active = await db.add_source("current", SourceType.REPLACEMENTS, "unused", 200, None, None, "2026-2027", None)
    disabled = await db.add_source("archive", SourceType.REPLACEMENTS, "unused", 100, None, None, "2026-2027", None)
    for sid, day in [(active, date(2026, 11, 2)), (disabled, date(2026, 9, 30))]:
        await db.replace_source_snapshot(
            sid, SourceType.REPLACEMENTS, [], [ParsedReplacement(day, "11а", 4, "Информатика", None, "301")]
        )
    await db.set_source_enabled(disabled, False)
    await db.source_status(active, "error", "Источник временно недоступен")

    async with TestClient(TestServer(make_app(db))) as client:
        response = await client.get("/health")
        data = await response.json()

    assert response.status == 200
    assert data["status"] == "degraded"
    assert data["source_errors"] == 1
    assert data["replacements"] == {"count": 1, "first_date": "2026-11-02", "last_date": "2026-11-02"}
