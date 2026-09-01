from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.db import Database
from app.domain import EffectiveLesson, PublicationState
from app.services.notifications import NotificationService


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.messages.append((chat_id, text, parse_mode))


class FakeSchedules:
    async def day_schedule(self, _class_name, _profile, _day):
        lesson = EffectiveLesson(1, "Алгебра", "101", "Иванов", time(10), time(10, 45), True, False)
        return {"state": "ready", "lessons": [lesson], "bells_known": True}


def settings(path: Path) -> Settings:
    return Settings("", frozenset(), None, "Europe/Moscow", "2026-2027", path, 10000, 10, 30, 15, {}, {}, {}, {})


@pytest.mark.asyncio
async def test_notifications_are_sent_only_once(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    await db.init()
    await db.upsert_user(42, "2026-2027", "10а", None, 15)
    await db.set_publication_state(PublicationState.READY)
    fixed_now = datetime(2026, 9, 2, 9, 50, tzinfo=ZoneInfo("Europe/Moscow"))
    monkeypatch.setattr("app.services.notifications.now_local", lambda _tz: fixed_now)
    bot = FakeBot()
    service = NotificationService(db, settings(tmp_path / "db.sqlite"), FakeSchedules())

    await service.tick(bot)
    await service.tick(bot)

    assert len(bot.messages) == 2
    assert any("Скоро 1-й урок" in text for _, text, _ in bot.messages)
    assert any("Изменение в расписании" in text for _, text, _ in bot.messages)
