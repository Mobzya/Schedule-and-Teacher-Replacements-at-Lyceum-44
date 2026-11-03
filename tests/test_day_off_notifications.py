import asyncio
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.config import settings_from_mapping
from app.db import Database
from app.domain import PublicationState
from app.services.calendar import CalendarService
from app.services.notifications import NotificationService
from app.services.schedule import ScheduleService


class Bot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.messages.append((chat_id, text))


async def runtime(path: Path, monkeypatch, moment: str):
    settings = settings_from_mapping({"BOT_TOKEN": "test"})
    db = Database(path)
    await db.init()
    await db.upsert_user(42, settings.academic_year, "10а")
    await db.set_publication_state(PublicationState.READY)
    monkeypatch.setattr(
        "app.services.notifications.now_local",
        lambda _: datetime.fromisoformat(moment).replace(tzinfo=ZoneInfo("Europe/Moscow")),
    )
    # No schedule snapshot: holidays must not require today's lessons to exist.
    schedules = ScheduleService(db, settings, CalendarService(db))
    return db, NotificationService(db, settings, schedules)


@pytest.mark.asyncio
async def test_evening_greeting_does_not_repeat_after_ticks_and_restart(tmp_path, monkeypatch):
    path = tmp_path / "greetings.sqlite"
    db, service = await runtime(path, monkeypatch, "2026-11-03T19:00:00")
    bot = Bot()
    await service.tick(bot, user_limit=2)
    await service.tick(bot, user_limit=2)
    restarted_db = Database(path)
    await restarted_db.init()
    restarted = NotificationService(restarted_db, service.settings, service.schedules)
    await restarted.tick(bot, user_limit=2)

    assert len(bot.messages) == 1
    assert "Поздравляю с наступающим Днём народного единства! 🇷🇺" in bot.messages[0][1]
    assert await db.notification_sent(42, "dayoff:2026-11-04")
    assert not await db.pending_notifications(20)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "moment,expected",
    [
        ("2026-11-03T18:59:00", None),
        ("2026-11-03T23:50:00", "Днём народного единства"),
        ("2026-10-23T19:00:00", "осенних каникул"),
        ("2026-10-24T19:00:00", None),
        ("2027-03-21T19:00:00", "весенних каникул"),  # Sunday before a vacation
        ("2027-03-07T19:00:00", "Международным женским днём"),
        ("2026-12-31T19:00:00", "Новым годом"),  # already on winter vacation
        ("2027-01-01T19:00:00", None),
        ("2026-11-06T19:00:00", None),  # ordinary weekend
    ],
)
async def test_greetings_run_in_evening_even_on_non_school_days(tmp_path, monkeypatch, moment, expected):
    _, service = await runtime(tmp_path / "evening.sqlite", monkeypatch, moment)
    bot = Bot()
    await service.tick(bot)
    if expected:
        assert len(bot.messages) == 1 and expected in bot.messages[0][1]
    else:
        assert not bot.messages


@pytest.mark.asyncio
async def test_additional_first_grade_break_does_not_congratulate_other_grades(tmp_path, monkeypatch):
    db, service = await runtime(tmp_path / "classes.sqlite", monkeypatch, "2027-02-19T19:00:00")
    await db.upsert_user(84, service.settings.academic_year, "1а")
    bot = Bot()
    await service.tick(bot)
    assert [uid for uid, _ in bot.messages] == [84]
    assert "дополнительных каникул" in bot.messages[0][1]


@pytest.mark.asyncio
async def test_inactive_or_unsubscribed_users_do_not_receive_greetings(tmp_path, monkeypatch):
    db, service = await runtime(tmp_path / "subscribers.sqlite", monkeypatch, "2026-11-03T19:00:00")
    for field in ("notifications", "morning_summary", "replacement_notifications"):
        await db.update_user_setting(42, field, 0)
    await db.upsert_user(84, "2025-2026", "10а")
    await db.upsert_user(126, service.settings.academic_year)
    bot = Bot()
    await service.tick(bot, user_limit=2)
    assert not bot.messages
    assert not await db.pending_notifications(20)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["schoolday", "disabled", "waiting", "other_class", "other_year"])
async def test_queued_greeting_is_revalidated_before_delivery(tmp_path, monkeypatch, change):
    db, service = await runtime(tmp_path / "revalidate.sqlite", monkeypatch, "2027-02-19T19:00:00")
    await db.set_user_class(42, "1а", None, service.settings.academic_year, 15)
    day = date(2027, 2, 20)
    await db.enqueue_notification(42, f"dayoff:{day.isoformat()}", "Устаревший текст")
    if change == "schoolday":
        await db.set_calendar_day(day, True, 0, "Учимся", 1)
    elif change == "disabled":
        for field in ("notifications", "morning_summary", "replacement_notifications"):
            await db.update_user_setting(42, field, 0)
    elif change == "waiting":
        await db.set_publication_state(PublicationState.WAITING)
    elif change == "other_class":
        await db.set_user_class(42, "11а", None, service.settings.academic_year, 15)
    else:
        await db.upsert_user(42, "2027-2028", "2а")
    bot = Bot()
    await service.dispatch_outbox(bot, 20)
    assert not bot.messages
    assert not await db.pending_notifications(20)


@pytest.mark.asyncio
async def test_greeting_is_not_delivered_late_next_morning(tmp_path, monkeypatch):
    db, service = await runtime(tmp_path / "stale.sqlite", monkeypatch, "2026-11-04T08:00:00")
    await db.enqueue_notification(42, "dayoff:2026-11-04", "Вчерашнее поздравление")
    await db.enqueue_notification(42, "dayoff:invalid", "Некорректная дата")
    await db.enqueue_notification(42, "broadcast:important", "Объявление")
    bot = Bot()
    await service.dispatch_outbox(bot, 20)
    assert bot.messages == [(42, "Объявление")]
    assert not await db.pending_notifications(20)


@pytest.mark.asyncio
async def test_overlapping_senders_do_not_duplicate_greeting(tmp_path, monkeypatch):
    db, service = await runtime(tmp_path / "concurrent.sqlite", monkeypatch, "2026-11-03T19:00:00")
    await db.enqueue_notification(42, "dayoff:2026-11-04", "Поздравление")
    # Both senders read the same pending row before either can finish it.
    pending = await db.pending_notifications(20)
    monkeypatch.setattr(db, "pending_notifications", AsyncMock(return_value=pending))
    bot = Bot()
    await asyncio.gather(service.dispatch_outbox(bot, 20), service.dispatch_outbox(bot, 20))
    assert len(bot.messages) == 1
    assert await db.notification_sent(42, "dayoff:2026-11-04")


@pytest.mark.asyncio
async def test_failed_delivery_can_retry_without_creating_another_message(tmp_path, monkeypatch):
    db, service = await runtime(tmp_path / "retry.sqlite", monkeypatch, "2026-11-03T19:00:00")
    bot = Bot()
    monkeypatch.setattr(bot, "send_message", AsyncMock(side_effect=RuntimeError("Временный сбой Telegram")))
    await service.tick(bot)
    assert not await db.notification_sent(42, "dayoff:2026-11-04")
    async with db.connect() as connection:
        await connection.execute("UPDATE notification_outbox SET next_attempt_at='2000-01-01T00:00:00+00:00'")
        await connection.commit()
    delivered = Bot()
    await service.tick(delivered)
    await service.tick(delivered)
    assert len(delivered.messages) == 1
    assert await db.notification_sent(42, "dayoff:2026-11-04")
