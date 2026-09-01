import os
from pathlib import Path

import pytest

from app.db import Database
from app.domain import ParsedLesson, SourceType


@pytest.mark.asyncio
async def test_postgres_backend_round_trip():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not configured")

    db = Database(Path("unused.sqlite"), database_url)
    await db.init()
    async with db.connect() as connection:
        for table in ("audit_log", "weekly_lessons", "data_sources", "users", "app_state"):
            await connection.execute(f"DELETE FROM {table}")
        await connection.commit()

    await db.upsert_user(42, "2026-2027", "10а", None, 15)
    assert (await db.get_user(42))["class_name"] == "10а"

    source_id = await db.add_source(
        "postgres-test", SourceType.WEEKLY, "https://example.invalid", 100, None, None, "2026-2027", 42
    )
    await db.replace_source_snapshot(
        source_id,
        SourceType.WEEKLY,
        [ParsedLesson("10а", None, 0, None, 1, "Алгебра", "316")],
        [],
    )
    assert ("10а", None) in await db.classes()
