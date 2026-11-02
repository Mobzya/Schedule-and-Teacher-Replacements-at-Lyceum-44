from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.db import Database
from app.domain import EffectiveLesson, PublicationState
from app.services.calendar import CalendarService
from app.services.notifications import NotificationService
from app.services.schedule import ScheduleService


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


@pytest.mark.asyncio
async def test_broadcast_messages_are_durably_queued_per_user(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    await db.init()
    await db.upsert_user(42, "2026-2027")
    await db.upsert_user(84, "2026-2027")

    queued = await db.enqueue_notifications([42, 84], "broadcast:test", "Announcement")
    first = await db.pending_notifications(limit=1)
    second = await db.pending_notifications(limit=2)

    assert queued == 2
    assert len(first) == 1
    assert {item["user_id"] for item in second} == {42, 84}
    assert all(item["message"] == "Announcement" for item in second)


@pytest.mark.asyncio
async def test_replacements_are_grouped_and_do_not_repeat(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    await db.init()
    await db.upsert_user(42, "2026-2027", "10а")
    user = await db.get_user(42)
    now = datetime(2026, 10, 19, 9, 50, tzinfo=ZoneInfo("Europe/Moscow"))
    lessons = [
        EffectiveLesson(1, "Алгебра", "101", None, time(10), time(10, 45), True),
        EffectiveLesson(2, "Физика", "102", None, time(11), time(11, 45), True),
    ]
    service = NotificationService(db, settings(tmp_path / "db.sqlite"), FakeSchedules())
    await service._changes(user, {"lessons": lessons}, now)
    await service._changes(user, {"lessons": lessons}, now)
    queued = await db.pending_notifications(10)
    assert len(queued) == 1
    assert "Алгебра" in queued[0]["message"] and "Физика" in queued[0]["message"]


@pytest.mark.asyncio
async def test_no_late_replacement_alert_for_finished_lessons(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    await db.init()
    await db.upsert_user(42, "2026-2027", "10а")
    service = NotificationService(db, settings(tmp_path / "db.sqlite"), FakeSchedules())
    info = await FakeSchedules().day_schedule(None, None, None)
    await service._changes(await db.get_user(42), info, datetime(2026, 10, 19, 22))
    assert not await db.pending_notifications(10)


@pytest.mark.asyncio
async def test_inactive_imported_users_do_not_delay_active_users(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    db = Database(tmp_path / "notification.sqlite")
    await db.init()
    for uid in range(1, 51):
        await db.upsert_user(uid, "2026-2027")  # imported, class not selected
    for uid in (100, 200, 300):
        await db.set_user_class(uid, "10а", None, "2026-2027", 15)
    for field in ("notifications", "morning_summary", "replacement_notifications"):
        await db.update_user_setting(300, field, 0)
    await db.set_publication_state(PublicationState.READY)
    monkeypatch.setattr(
        "app.services.notifications.now_local", lambda _: datetime(2026, 9, 2, 9, 50, tzinfo=ZoneInfo("Europe/Moscow"))
    )
    schedules = FakeSchedules()
    schedules.day_schedule = AsyncMock(wraps=schedules.day_schedule)
    service = NotificationService(db, settings(tmp_path / "notification.sqlite"), schedules)
    await service.tick(FakeBot(), user_limit=2)
    assert await db.get_state("notification_cursor") == "200"
    assert schedules.day_schedule.await_count == 1  # reuse the same class
    await service.tick(FakeBot(), user_limit=2)  # cursor wraps over active users
    assert await db.get_state("notification_cursor") == "200"
    assert {row["user_id"] for row in await db.pending_notifications(10)} <= {100, 200}


@pytest.mark.asyncio
@pytest.mark.parametrize("day", ["2026-10-26", "2026-12-28", "2027-03-23", "2027-05-03", "2027-05-10", "2027-06-02"])
async def test_no_academic_notifications_on_non_school_days_but_broadcasts_work(tmp_path, monkeypatch, day):
    from unittest.mock import AsyncMock

    db = Database(tmp_path / "holidays.sqlite")
    await db.init()
    await db.upsert_user(42, "2026-2027", "10а")
    await db.set_publication_state(PublicationState.READY)
    now = datetime.fromisoformat(f"{day}T09:50:00").replace(tzinfo=ZoneInfo("Europe/Moscow"))
    monkeypatch.setattr("app.services.notifications.now_local", lambda _: now)
    # Include previously queued messages: closing the calendar must block delivery,
    # not merely stop creating new notices. School-wide announcements still work.
    for kind in ("lesson", "morning", "change"):
        await db.enqueue_notification(42, f"{kind}:{day}:test", "Не отправлять")
    await db.enqueue_notification(42, "broadcast:important", "Объявление администрации")
    schedules = FakeSchedules()
    schedules.day_schedule = AsyncMock(wraps=schedules.day_schedule)
    bot = FakeBot()
    await NotificationService(db, settings(tmp_path / "holidays.sqlite"), schedules).tick(bot)
    assert [text for _, text, _ in bot.messages] == ["Объявление администрации"]
    assert not await db.pending_notifications(20)
    schedules.day_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_delayed_academic_notices_do_not_escape_on_next_school_day(tmp_path, monkeypatch):
    db = Database(tmp_path / "delayed.sqlite")
    await db.init()
    await db.upsert_user(42, "2026-2027", "10а")
    await db.set_publication_state(PublicationState.READY)
    now = datetime(2026, 11, 2, 9, 50, tzinfo=ZoneInfo("Europe/Moscow"))
    monkeypatch.setattr("app.services.notifications.now_local", lambda _: now)
    for kind in ("lesson", "morning", "change"):
        await db.enqueue_notification(42, f"{kind}:2026-10-23:old", "Устаревшее")
    await db.enqueue_notification(42, "lesson:invalid", "Неверная дата")
    await db.enqueue_notification(42, "broadcast:delayed", "Важное объявление")
    service = NotificationService(db, settings(tmp_path / "delayed.sqlite"), FakeSchedules())
    bot = FakeBot()
    await service.dispatch_outbox(bot, 20)
    assert [text for _, text, _ in bot.messages] == ["Важное объявление"]
    assert not await db.pending_notifications(20)
    await service.tick(bot)
    assert any("Скоро 1-й урок" in text for _, text, _ in bot.messages)


@pytest.mark.asyncio
async def test_first_grade_break_is_not_applied_to_eleventh_grade(tmp_path, monkeypatch):
    db = Database(tmp_path / "first-grade.sqlite")
    await db.init()
    await db.set_publication_state(PublicationState.READY)
    for uid, class_name in ((42, "1а"), (84, "11а")):
        await db.upsert_user(uid, "2026-2027", class_name)
        await db.enqueue_notification(uid, "morning:2027-02-24", "Утреннее расписание")
    now = datetime(2027, 2, 24, 7, tzinfo=ZoneInfo("Europe/Moscow"))
    monkeypatch.setattr("app.services.notifications.now_local", lambda _: now)
    st = settings(tmp_path / "first-grade.sqlite")
    real_schedule = ScheduleService(db, st, CalendarService(db))
    assert (await real_schedule.day_schedule("1а", None, now.date()))["state"] == "day_off"
    assert (await real_schedule.day_schedule("11а", None, now.date()))["state"] == "no_class_data"
    bot = FakeBot()
    await NotificationService(db, st, real_schedule).dispatch_outbox(bot, 20)
    assert [uid for uid, _, _ in bot.messages] == [84]


@pytest.mark.asyncio
async def test_admin_can_explicitly_enable_lessons_during_vacation(tmp_path, monkeypatch):
    db = Database(tmp_path / "override.sqlite")
    await db.init()
    await db.upsert_user(42, "2026-2027", "10а")
    await db.set_publication_state(PublicationState.READY)
    day = date(2026, 10, 26)
    await db.set_calendar_day(day, True, 0, "Разовые занятия", 1)
    now = datetime(2026, 10, 26, 9, 50, tzinfo=ZoneInfo("Europe/Moscow"))
    monkeypatch.setattr("app.services.notifications.now_local", lambda _: now)
    bot = FakeBot()
    await NotificationService(db, settings(tmp_path / "override.sqlite"), FakeSchedules()).tick(bot)
    assert any("Скоро 1-й урок" in text for _, text, _ in bot.messages)
