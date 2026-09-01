from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from app.db import Database
from app.domain import PublicationState, SourceType


async def run():
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")
        await db.init()
        assert await db.publication_state() == PublicationState.WAITING
        await db.upsert_user(123, "2026-2027", default_reminder=15)
        assert (await db.get_user(123))["user_id"] == 123
        sid = await db.add_source(
            "test",
            SourceType.WEEKLY,
            "https://docs.google.com/spreadsheets/d/abc/edit#gid=0",
            100,
            None,
            None,
            "2026-2027",
            1,
        )
        assert sid > 0 and len(await db.list_sources()) == 1
        await db.set_publication_state(PublicationState.READY, 1)
        assert await db.publication_state() == PublicationState.READY
    print("SELF_CHECK PASS")


if __name__ == "__main__":
    asyncio.run(run())
