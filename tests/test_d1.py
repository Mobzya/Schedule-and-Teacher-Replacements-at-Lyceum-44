import sqlite3
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import SCHEMA, Database
from app.domain import ParsedLesson, ParsedReplacement, SourceType


class Statement:
    def __init__(self, binding, query, params=()):
        self.binding, self.query, self.params = binding, query, params

    def bind(self, *params):
        return Statement(self.binding, self.query, params)

    async def all(self):
        cursor = self.binding.connection.execute(self.query, self.params)
        rows = [dict(row) for row in cursor.fetchall()]
        self.binding.connection.commit()
        return SimpleNamespace(
            results=rows, meta=SimpleNamespace(last_row_id=cursor.lastrowid, changes=cursor.rowcount)
        )


class D1Binding:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.batch_sizes = []

    def prepare(self, query):
        return Statement(self, query)

    async def batch(self, statements):
        self.batch_sizes.append(len(statements))
        try:
            self.connection.execute("BEGIN")
            for statement in statements:
                self.connection.execute(statement.query, statement.params)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise


@pytest.mark.asyncio
@pytest.mark.parametrize("source_type", list(SourceType))
async def test_d1_snapshot_uses_one_atomic_bulk_insert(source_type):
    binding = D1Binding()
    try:
        db = Database(Path("unused"), d1_binding=binding)
        sid = await db.add_source("test", source_type, "unused", 100, None, None, "2026-2027", 1)
        subject = 'Русский "язык" & <литература>'
        lessons = [ParsedLesson("5а", None, 0, date(2026, 10, 19), n, subject, "101") for n in range(1, 301)]
        replacements = [ParsedReplacement(date(2026, 10, 19), "5а", n, subject, None, "101") for n in range(1, 301)]
        await db.replace_source_snapshot(sid, source_type, lessons, replacements)
        table = {
            SourceType.WEEKLY: "weekly_lessons",
            SourceType.DATE: "dated_lessons",
            SourceType.REPLACEMENTS: "replacements",
        }[source_type]
        rows = binding.connection.execute(f"SELECT * FROM {table}").fetchall()
        assert len(rows) == 300
        assert rows[0]["subject"] == subject
        assert rows[0]["teacher"] is None
        assert binding.batch_sizes == [2]
    finally:
        binding.connection.close()


@pytest.mark.asyncio
async def test_d1_invalid_snapshot_rolls_back_deletion():
    binding = D1Binding()
    try:
        db = Database(Path("unused"), d1_binding=binding)
        sid = await db.add_source("test", SourceType.WEEKLY, "unused", 100, None, None, "2026-2027", 1)
        old = ParsedLesson("5а", None, 0, None, 1, "Математика", "101")
        await db.replace_source_snapshot(sid, SourceType.WEEKLY, [old], [])
        invalid = ParsedLesson("5а", None, None, None, 1, "Новые данные", "102")
        with pytest.raises(sqlite3.IntegrityError):
            await db.replace_source_snapshot(sid, SourceType.WEEKLY, [invalid], [])
        row = binding.connection.execute("SELECT subject FROM weekly_lessons").fetchone()
        assert row["subject"] == "Математика"
    finally:
        binding.connection.close()


@pytest.mark.asyncio
async def test_d1_notification_lease_allows_one_sender_and_retry_after_expiry():
    binding = D1Binding()
    try:
        db = Database(Path("unused"), d1_binding=binding)
        await db.enqueue_notification(42, "dayoff:2026-11-04", "Поздравление")
        assert await db.claim_notification(42, "dayoff:2026-11-04")
        assert not await db.claim_notification(42, "dayoff:2026-11-04")
        assert not await db.pending_notifications(20)
        # A crashed sender's lease expires; another Cron can resume delivery.
        binding.connection.execute("UPDATE notification_outbox SET next_attempt_at='2000-01-01T00:00:00+00:00'")
        binding.connection.commit()
        assert await db.claim_notification(42, "dayoff:2026-11-04")
        await db.finish_notification(42, "dayoff:2026-11-04")
        await db.enqueue_notification(42, "dayoff:2026-11-04", "Повторное поздравление")
        assert await db.notification_sent(42, "dayoff:2026-11-04")
        assert not await db.pending_notifications(20)
    finally:
        binding.connection.close()
