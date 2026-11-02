import sqlite3
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from test_d1 import D1Binding
from test_worker_ui import Bot, buttons, event, runtime
from test_worker_ui import worker as worker_fixture

from app.db import Database
from app.services.friends import FriendsService, validate_label
from scripts.import_cloudflare_users import import_sql, normalize_users

worker = worker_fixture


NOW = datetime(2026, 10, 19, 8, 30, tzinfo=ZoneInfo("Europe/Moscow"))


@pytest.mark.parametrize("value", ["", "@bad", "@12345", "a" * 49, "A\nB"])
def test_invalid_labels(value):
    with pytest.raises(ValueError):
        validate_label(value)


def test_names_and_usernames():
    assert validate_label("  Анна   Петрова  ") == "Анна Петрова"
    assert validate_label("@friend_name") == "@friend_name"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "d1"])
async def test_crud_duplicate_limit_and_ownership(tmp_path, backend):
    binding = D1Binding() if backend == "d1" else None
    db = Database(tmp_path / "friends.db", d1_binding=binding)
    await db.init()
    for owner in (2, 3):
        await db.upsert_user(owner, "2026-2027")
    try:
        assert await db.save_friend(2, "Анна", "5а", None, "2026-2027")
        friend_id = (await db.list_friends(2))[0]["id"]
        assert not await db.save_friend(2, "АННА", "5а", None, "2026-2027")
        assert await db.save_friend(3, "Анна", "11а", None, "2026-2027")
        assert await db.get_friend(3, friend_id) is None
        assert not await db.save_friend(3, "Взлом", "11а", None, "2026-2027", friend_id)
        await db.delete_friend(3, friend_id)
        assert (await db.get_friend(2, friend_id))["label"] == "Анна"
        assert await db.save_friend(2, "Аня", "11а", "профиль", "2026-2027", friend_id)
        for i in range(29):
            assert await db.save_friend(2, f"Друг {i}", "5а", None, "2026-2027")
        assert not await db.save_friend(2, "Лишний", "5а", None, "2026-2027")
        assert len(await db.list_friends(2)) == 30
        await db.delete_friend(2, friend_id)
        assert await db.get_friend(2, friend_id) is None
    finally:
        if binding:
            binding.connection.close()


@pytest.mark.asyncio
async def test_friend_flow_does_not_change_owner_class(tmp_path):
    db, settings, schedules = await runtime(tmp_path)
    await db.set_user_class(2, "11а", None, settings.academic_year, 15)
    service = FriendsService(db, settings, schedules)
    view = await service.callback(2, "f:add", None, {}, NOW)
    assert view[2] == "friend:name"
    view = await service.message(2, "Аня <&>", view[3], NOW)
    assert "Аня &lt;&amp;&gt;" in view[0]
    view = await service.callback(2, "f:g:5", view[2], view[3], NOW)
    assert "f:c:5а" in buttons(view[1])
    view = await service.callback(2, "f:c:5а", view[2], view[3], NOW)
    assert view[2] is None
    friend = (await db.list_friends(2))[0]
    assert friend["class_name"] == "5а"
    assert (await db.get_user(2))["class_name"] == "11а"
    fid = friend["id"]
    view = await service.callback(2, f"f:rename:{fid}:0", None, {}, NOW)
    await service.message(2, "@friend_name", view[3], NOW)
    assert (await db.get_friend(2, fid))["label"] == "@friend_name"
    view = await service.callback(2, f"f:delete:{fid}:0", None, {}, NOW)
    assert await db.get_friend(2, fid) is not None  # confirmation required
    await service.callback(2, f"f:remove:{fid}:0", None, {}, NOW)
    assert not await db.list_friends(2)


@pytest.mark.asyncio
async def test_paging_current_next_and_per_class_cache(tmp_path):
    db, settings, schedules = await runtime(tmp_path)
    await db.upsert_user(2, settings.academic_year)
    for i in range(7):
        await db.save_friend(2, f"Друг {i}", "5а", None, settings.academic_year)
    schedules.current_and_next = AsyncMock(wraps=schedules.current_and_next)
    service = FriendsService(db, settings, schedules)
    view = await service.page(2, 0, NOW)
    assert "Сейчас:" in view[0] and "Математика" in view[0] and "Далее:" in view[0]
    assert "Друг 2" in view[0] and "Друг 3" not in view[0]
    assert "f:list:1" in buttons(view[1])
    assert schedules.current_and_next.await_count == 1
    view = await service.page(2, 1, NOW)
    assert "Друг 3" in view[0] and "Друг 6" not in view[0]
    assert {"f:list:0", "f:list:2"} <= buttons(view[1]).keys()
    await db.delete_friend(2, (await db.list_friends(2))[-1]["id"])
    view = await service.page(2, 999, NOW)
    assert "↻ 2/2" in buttons(view[1]).values()
    assert len(view[0]) < 4096


@pytest.mark.asyncio
async def test_no_data_day_off_and_new_year_are_not_finished_lessons(tmp_path):
    db, settings, schedules = await runtime(tmp_path)
    await db.upsert_user(2, settings.academic_year)
    service = FriendsService(db, settings, schedules)
    await db.save_friend(2, "Без данных", "99я", None, settings.academic_year)
    view = await service.page(2, 0, NOW)
    assert "закончились" not in view[0] and "ℹ️" in view[0]
    view = await service.page(2, 0, NOW.replace(day=18))  # Sunday
    assert "закончились" not in view[0]
    await db.save_friend(2, "Старый класс", "5а", None, "2025-2026")
    view = await service.page(2, 0, NOW)
    assert "Новый учебный год" in view[0]


@pytest.mark.asyncio
async def test_stale_draft_and_cross_owner_callbacks_are_rejected(tmp_path):
    db, settings, schedules = await runtime(tmp_path)
    await db.upsert_user(2, settings.academic_year)
    await db.upsert_user(3, settings.academic_year)
    await db.save_friend(2, "Аня", "5а", None, settings.academic_year)
    service = FriendsService(db, settings, schedules)
    fid = (await db.list_friends(2))[0]["id"]
    for action in ("edit", "rename", "class", "delete", "remove"):
        with pytest.raises(ValueError):
            await service.callback(3, f"f:{action}:{fid}:0", None, {}, NOW)
    with pytest.raises(ValueError):
        await service.callback(2, "f:c:5а", None, {}, NOW)
    with pytest.raises(ValueError):
        await service.message(2, "АНЯ", {}, NOW)


@pytest.mark.asyncio
async def test_worker_non_admin_can_add_friend_and_cancel(worker, tmp_path, monkeypatch):
    db, settings, schedules = await runtime(tmp_path)
    bot, fsm = Bot(), {"state": None, "data": {}}

    async def get(*args):
        return fsm.copy()

    async def put(env, chat, user, state, data=None):
        fsm.update(state=state, data=data or {})

    monkeypatch.setattr(worker, "_runtime", AsyncMock(return_value=(settings, db, bot, schedules, None, None)))
    monkeypatch.setattr(worker, "_get_fsm", get)
    monkeypatch.setattr(worker, "_set_fsm", put)

    async def click(data):
        await worker._process_update({"callback_query": event(data)}, None)

    await click("f:add")
    assert fsm["state"] == "friend:name"
    await worker._handle_message(
        {"from": {"id": 2}, "chat": {"id": 2}, "text": "Друг"}, None, settings, db, bot, schedules, None
    )
    assert fsm["state"] == "friend:class"
    await click("f:c:5а")
    assert len(await db.list_friends(2)) == 1 and fsm["state"] is None
    await click("f:add")
    await worker._handle_message(
        {"from": {"id": 2}, "chat": {"id": 2}, "text": "/cancel"}, None, settings, db, bot, schedules, None
    )
    assert fsm["state"] is None
    await click("settings")
    assert "f:manage:0" in buttons(bot.edited[-1][2])
    assert "setting:notifications" not in buttons(bot.edited[-1][2])
    assert not bot.sent  # editing one panel, including /cancel


def test_import_fifty_users_is_idempotent_and_preserves_existing_preferences():
    from app.db import SCHEMA

    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    rows = [
        {"user_id": i, "class_name": "5а", "profile": None, "academic_year": "2026-2027", "notify": 0}
        for i in range(1, 51)
    ]
    users = normalize_users(rows, "2026-2027", {("5а", None)})
    db.executescript(import_sql(users))
    db.execute("UPDATE users SET notifications=1,class_name='11а' WHERE user_id=1")
    db.executescript(import_sql(users))
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 50
    assert db.execute("SELECT notifications,class_name FROM users WHERE user_id=1").fetchone() == (1, "11а")
    assert db.execute("SELECT notifications FROM users WHERE user_id=2").fetchone()[0] == 0
    stale = normalize_users([{**rows[0], "academic_year": "2025-2026"}], "2026-2027", {("5а", None)})
    assert stale[0]["class_name"] is None
    db.close()


def test_friends_migrations_match_initialized_schema():
    db = sqlite3.connect(":memory:")
    for path in sorted((Path(__file__).parents[1] / "cloudflare/migrations").glob("*.sql")):
        db.executescript(path.read_text())
    columns = {r[1] for r in db.execute("PRAGMA table_info(friends)")}
    assert {"id", "owner_id", "label", "label_key", "academic_year", "profile"} <= columns
    db.close()


@pytest.mark.asyncio
async def test_local_aiogram_routes_commands_and_persistent_friend_input(tmp_path, monkeypatch):
    from aiogram import Bot as AiogramBot
    from aiogram import Dispatcher
    from aiogram.types import Message, Update

    from app.handlers import admin, friends, student

    db, settings, schedules = await runtime(tmp_path)
    bot = AiogramBot("123456:test")
    sent = []

    async def request(bot, method, timeout=None):
        sent.append(method)
        if method.__api_method__ == "answerCallbackQuery":
            return True
        return Message.model_validate(
            {
                "message_id": 42,
                "date": 1792390000,
                "chat": {"id": 2, "type": "private"},
                "text": getattr(method, "text", ""),
            },
            context={"bot": bot},
        )

    monkeypatch.setattr(bot.session, "make_request", request)
    dispatcher = Dispatcher()
    dispatcher.include_routers(admin.router, student.router, friends.router)
    serial = 0

    async def send(text=None, callback=None):
        nonlocal serial
        serial += 1
        message = {
            "message_id": 42,
            "date": 1792390000,
            "from": {"id": 2, "is_bot": False, "first_name": "Ученик"},
            "chat": {"id": 2, "type": "private"},
            "text": text or "panel",
        }
        value = (
            {"update_id": serial, "message": message}
            if text
            else {
                "update_id": serial,
                "callback_query": {
                    "id": str(serial),
                    "chat_instance": "chat",
                    "from": message["from"],
                    "message": message,
                    "data": callback,
                },
            }
        )
        await dispatcher.feed_update(
            bot, Update.model_validate(value, context={"bot": bot}), db=db, settings=settings, schedules=schedules
        )

    try:
        await send(text="/start")
        await send(callback="f:add")
        await send(text="Аня")
        await send(callback="f:g:5")
        await send(callback="f:c:5а")
        assert len(await db.list_friends(2)) == 1
        await send(text="/friends")
        assert any("Аня" in (getattr(m, "text", "") or "") for m in sent)
        await send(callback="f:add")
        await send(text="/cancel")
        await send(text="Не нужно добавлять")
        assert len(await db.list_friends(2)) == 1
        await send(callback="f:add")
        await send(callback="help")  # leaving a draft by an old menu button
        await send(text="Тоже не добавлять")
        assert len(await db.list_friends(2)) == 1
        await send(callback="settings")
        last = next(m for m in reversed(sent) if getattr(m, "reply_markup", None))
        assert "f:manage:0" in {b.callback_data for row in last.reply_markup.inline_keyboard for b in row}
    finally:
        await dispatcher.storage.close()
        await bot.session.close()
