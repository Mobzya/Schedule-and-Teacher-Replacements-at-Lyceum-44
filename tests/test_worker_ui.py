import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.config import settings_from_mapping
from app.db import Database
from app.domain import ParsedLesson, PublicationState, SourceType
from app.services.calendar import CalendarService
from app.services.schedule import ScheduleService


@pytest.fixture
def worker(monkeypatch):
    # Only the Cloudflare transport is stubbed: callbacks use real SQLite data,
    # schedule selection and rendering. No Telegram or production bindings.
    js = ModuleType("js")
    js.AbortSignal = js.Array = js.File = js.FormData = js.Object = js.fetch = None
    ffi = ModuleType("pyodide.ffi")
    ffi.to_js = lambda value, **kwargs: value
    sdk = ModuleType("workers")
    sdk.Response = sdk.WorkerEntrypoint = type("WorkerEntrypoint", (), {})
    for name, module in (("js", js), ("pyodide.ffi", ffi), ("workers", sdk)):
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location(
        "worker_under_test", Path(__file__).parents[1] / "cloudflare/worker.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "now_local", lambda tz: datetime(2026, 10, 19, 12, tzinfo=ZoneInfo("Europe/Moscow")))
    return module


class Bot:
    def __init__(self):
        self.sent, self.edited, self.answered = [], [], []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((text, kwargs.get("reply_markup")))
        return {"message_id": 90}

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edited.append((message_id, text, kwargs.get("reply_markup")))
        return {"message_id": message_id}

    async def answer_callback(self, *args):
        self.answered.append(args)


def event(data, user=2):
    return {
        "id": "callback",
        "from": {"id": user, "first_name": "Ученик"},
        "message": {"message_id": 42, "chat": {"id": user}},
        "data": data,
    }


def buttons(keyboard):
    return {button["callback_data"]: button["text"] for row in keyboard["inline_keyboard"] for button in row}


async def runtime(tmp_path):
    settings = settings_from_mapping({"BOT_TOKEN": "test", "ADMIN_IDS": "1", "ACADEMIC_YEAR": "2026-2027"})
    db = Database(tmp_path / "ui.sqlite")
    await db.init()
    sid = await db.add_source("test", SourceType.WEEKLY, "unused", 100, None, None, "2026-2027", 1)
    await db.replace_source_snapshot(
        sid,
        SourceType.WEEKLY,
        [
            ParsedLesson("5а", None, 0, None, 1, "Математика", "101"),
            ParsedLesson("5а", None, 1, None, 1, "Физика", "102"),
            ParsedLesson("11а", None, 0, None, 1, "Химия", "301"),
        ],
        [],
    )
    await db.set_publication_state(PublicationState.READY)
    return db, settings, ScheduleService(db, settings, CalendarService(db))


@pytest.mark.asyncio
async def test_choose_class_day_week_and_bells_edit_one_panel(worker, tmp_path):
    db, settings, schedules = await runtime(tmp_path)
    bot = Bot()
    for data in ("class:choose", "grade:5", "classn:5а"):
        await worker._student_callback(event(data), db, settings, schedules, bot)
    assert (await db.get_user(2))["class_name"] == "5а"
    assert "Лицей №44" in bot.edited[-1][1]
    assert "today" in buttons(bot.edited[-1][2])
    await worker._student_callback(event("tomorrow"), db, settings, schedules, bot)
    assert "20.10" in bot.edited[-1][1] and "Физика" in bot.edited[-1][1]
    assert "day:2026-10-21" in buttons(bot.edited[-1][2])
    await worker._student_callback(event("week:0"), db, settings, schedules, bot)
    assert len([data for data in buttons(bot.edited[-1][2]) if data.startswith("day:")]) == 7
    await worker._student_callback(event("bells"), db, settings, schedules, bot)
    assert "08:15–09:00" in bot.edited[-1][1]
    assert not bot.sent
    assert all(edit[0] == 42 for edit in bot.edited)


@pytest.mark.asyncio
async def test_settings_stay_open_and_show_current_selection(worker, tmp_path):
    db, settings, schedules = await runtime(tmp_path)
    bot = Bot()
    await worker._student_callback(event("setting:notifications"), db, settings, schedules, bot)
    assert buttons(bot.edited[-1][2])["setting:notifications"].startswith("○")
    await worker._student_callback(event("remind:30"), db, settings, schedules, bot)
    assert buttons(bot.edited[-1][2])["remind:30"].startswith("✓")
    assert "Настройки" in bot.edited[-1][1]
    await worker._student_callback(event("remind:999"), db, settings, schedules, bot)
    assert (await db.get_user(2))["reminder_minutes"] == 30
    assert not bot.sent


@pytest.mark.asyncio
async def test_stale_class_button_does_not_save_nonexistent_class(worker, tmp_path):
    db, settings, schedules = await runtime(tmp_path)
    await db.set_user_class(2, "5а", None, settings.academic_year, 15)
    bot = Bot()
    await worker._student_callback(event("classn:99я"), db, settings, schedules, bot)
    assert (await db.get_user(2))["class_name"] == "5а"
    assert bot.answered[-1][2] is True


@pytest.mark.asyncio
async def test_profile_choice_returns_directly_to_home(worker, tmp_path):
    db, settings, schedules = await runtime(tmp_path)
    sid = await db.add_source("profiles", SourceType.WEEKLY, "unused", 100, None, None, "2026-2027", 1)
    await db.replace_source_snapshot(
        sid,
        SourceType.WEEKLY,
        [
            ParsedLesson("5е", "технологический", 0, None, 1, "Физика", "303"),
            ParsedLesson("5е", "гуманитарный", 0, None, 1, "История", "304"),
        ],
        [],
    )
    bot = Bot()
    await worker._student_callback(event("classn:5е"), db, settings, schedules, bot)
    choices = buttons(bot.edited[-1][2])
    callback_data = next(data for data, label in choices.items() if label == "технологический")
    await worker._student_callback(event(callback_data), db, settings, schedules, bot)
    assert (await db.get_user(2))["profile"] == "технологический"
    assert "Лицей №44" in bot.edited[-1][1]
    assert not bot.sent


@pytest.mark.asyncio
async def test_cancel_clears_draft_and_updates_existing_panel(worker, tmp_path, monkeypatch):
    db, settings, schedules = await runtime(tmp_path)
    bot = Bot()
    monkeypatch.setattr(
        worker,
        "_get_fsm",
        AsyncMock(
            return_value={
                "state": "broadcast_text",
                "data": {"panel_message_id": 42, "broadcast": "черновик"},
            }
        ),
    )
    clear = AsyncMock()
    monkeypatch.setattr(worker, "_set_fsm", clear)
    await worker._handle_message(
        {"from": {"id": 1}, "chat": {"id": 1}, "text": "/cancel"}, None, settings, db, bot, schedules, None
    )
    clear.assert_awaited_once_with(None, 1, 1, None)
    assert bot.edited[-1][0] == 42
    assert not bot.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/tomorrow", "/week", "/help"])
async def test_local_typed_commands_use_same_views(tmp_path, monkeypatch, command):
    from app.handlers import student

    db, settings, schedules = await runtime(tmp_path)
    await db.set_user_class(2, "5а", None, settings.academic_year, 15)
    monkeypatch.setattr(student, "now_local", lambda tz: datetime(2026, 10, 19, 12, tzinfo=tz))
    answer = AsyncMock()
    message = SimpleNamespace(text=command, from_user=SimpleNamespace(id=2, first_name="Ученик"), answer=answer)
    state = SimpleNamespace(clear=AsyncMock())
    await student.command_screen(message, db, settings, schedules, state)
    answer.assert_awaited_once()
    assert answer.call_args.kwargs["reply_markup"]
    state.clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_source_link_reuses_prompt_and_preserves_retry(worker, tmp_path, monkeypatch):
    db, settings, schedules = await runtime(tmp_path)
    bot, sync = Bot(), SimpleNamespace(preview=AsyncMock())
    data = {"source_type": SourceType.WEEKLY.value, "panel_message_id": 42}
    monkeypatch.setattr(worker, "_get_fsm", AsyncMock(return_value={"state": "source_url", "data": data}))
    save = AsyncMock()
    monkeypatch.setattr(worker, "_set_fsm", save)
    message = {"from": {"id": 1}, "chat": {"id": 1}, "text": "не ссылка"}
    await worker._handle_message(message, None, settings, db, bot, schedules, sync)
    assert not bot.sent and bot.edited[-1][0] == 42
    assert save.call_args.args[3] == "source_url"
    sync.preview.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_preview_escapes_text_and_reuses_panel(worker, tmp_path, monkeypatch):
    db, settings, schedules = await runtime(tmp_path)
    bot = Bot()
    monkeypatch.setattr(
        worker,
        "_get_fsm",
        AsyncMock(
            return_value={
                "state": "broadcast_text",
                "data": {"panel_message_id": 42},
            }
        ),
    )
    save = AsyncMock()
    monkeypatch.setattr(worker, "_set_fsm", save)
    await worker._handle_message(
        {"from": {"id": 1}, "chat": {"id": 1}, "text": "A < B & C"}, None, settings, db, bot, schedules, None
    )
    assert "A &lt; B &amp; C" in bot.edited[-1][1]
    assert not bot.sent
    assert save.call_args.args[4]["broadcast"] == "A < B & C"


@pytest.mark.asyncio
async def test_fsm_accepts_js_mapping_without_python_get(worker):
    class Row:
        def keys(self):
            return ("state", "data")

        def __getitem__(self, key):
            return {"state": "source_url", "data": '{"panel_message_id":42}'}[key]

    statement = SimpleNamespace(all=AsyncMock(return_value=SimpleNamespace(results=[Row()])))
    query = SimpleNamespace(bind=lambda *args: statement)
    env = SimpleNamespace(DB=SimpleNamespace(prepare=lambda *args: query))
    assert await worker._get_fsm(env, 1, 1) == {"state": "source_url", "data": {"panel_message_id": 42}}


@pytest.mark.asyncio
async def test_scheduled_uses_entrypoint_bindings(worker, monkeypatch):
    sync = SimpleNamespace(bootstrap=AsyncMock(), sync_next=AsyncMock())
    notices = SimpleNamespace(tick=AsyncMock())
    runtime_mock = AsyncMock(return_value=(None, None, "bot", None, sync, notices))
    monkeypatch.setattr(worker, "_runtime", runtime_mock)
    entry = worker.Default()
    entry.env = object()
    await entry.scheduled(SimpleNamespace(cron="*/2 * * * *"), object(), None)
    runtime_mock.assert_awaited_once_with(entry.env)
    sync.sync_next.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("error,sends", [("message is not modified", 0), ("message to edit not found", 1)])
async def test_panel_handles_unchanged_or_deleted_message(worker, error, sends):
    bot = Bot()
    bot.edit_message_text = AsyncMock(side_effect=RuntimeError(error))
    await worker._panel(bot, 2, "text", message_id=42)
    assert len(bot.sent) == sends
