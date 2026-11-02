from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from js import AbortSignal, Array, File, FormData, Object, fetch
from pyodide.ffi import to_js
from workers import Response, WorkerEntrypoint

from app.config import settings_from_mapping
from app.db import Database
from app.domain import PublicationState, SourceType
from app.services.avatars import AVATAR_CRON, AvatarService
from app.services.calendar import CalendarService
from app.services.friends import FriendsService
from app.services.notifications import NotificationService
from app.services.schedule import ScheduleService
from app.services.sync import SyncService
from app.source_urls import google_sheet_parts
from app.ui import (
    HELP_TEXT,
    bells_text,
    class_picker,
    day_markup,
    fit_lines,
    lesson_line,
    menu_markup,
    notification_settings_view,
    render_day,
    render_waiting,
    render_week,
    settings_view,
    week_markup,
)
from app.utils import RU_WEEKDAYS, esc, now_local

logger = logging.getLogger("lyceum44.worker")


def _value(env, key: str, default: str = "") -> str:
    value = getattr(env, key, default)
    return str(value) if value is not None else default


def _settings(env):
    names = (
        "BOT_TOKEN",
        "ADMIN_IDS",
        "TIMEZONE",
        "ACADEMIC_YEAR",
        "SOURCE_SYNC_MINUTES",
        "NOTIFICATION_TICK_SECONDS",
        "DEFAULT_REMINDER_MINUTES",
        "BOOTSTRAP_SCHEDULE_SOURCES_JSON",
        "BOOTSTRAP_REPLACEMENT_SOURCES_JSON",
        "BELL_SCHEDULES_JSON",
        "CLASS_BELL_SCHEDULES_JSON",
    )
    return settings_from_mapping({name: _value(env, name) for name in names})


def _button(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


def _kb(*rows: list[dict[str, str]]) -> dict:
    return {"inline_keyboard": list(rows)}


def _main_keyboard(is_admin: bool, class_selected: bool = True) -> dict:
    return menu_markup(is_admin, class_selected)


def _admin_keyboard() -> dict:
    return _kb(
        [_button("📡 Источники", "admin:sources"), _button("🔄 Синхронизация", "admin:sync")],
        [_button("🚦 Публикация", "admin:publication"), _button("📣 Рассылка", "admin:broadcast")],
        [_button("✏️ Сообщение ожидания", "admin:waiting_message")],
        [_button("← Ученический интерфейс", "home")],
    )


class TelegramClient:
    def __init__(self, token: str):
        self.token = token

    async def call(self, method: str, payload: dict | None = None):
        response = await fetch(
            f"https://api.telegram.org/bot{self.token}/{method}",
            to_js(
                {
                    "method": "POST",
                    "headers": {"content-type": "application/json"},
                    "body": json.dumps(payload or {}, ensure_ascii=False),
                    "signal": AbortSignal.timeout(25_000),
                },
                dict_converter=Object.fromEntries,
            ),
        )
        return await self._result(response)

    @staticmethod
    async def _result(response):
        result = (await response.json()).to_py()
        if not response.ok or not result.get("ok"):
            error = result.get("description", f"Telegram HTTP {response.status}")
            params = result.get("parameters") or {}
            from app.messaging import TelegramAPIError

            raise TelegramAPIError(error, result.get("error_code"), params.get("retry_after"))
        return result.get("result")

    async def set_profile_photo(self, blob, filename: str):
        # Native JS File/FormData avoids copying image bytes through Python and
        # the Pyodide Sequence conversion failure. Telegram requires a fresh JPG
        # multipart upload, not a URL or a cached Telegram file_id.
        form = FormData.new()
        form.append("photo", json.dumps({"type": "static", "photo": "attach://avatar"}))
        parts = Array.new()
        # Assets.fetch returns a workers.Response: blob() is an SDK wrapper,
        # not a native JS Blob. Passing it directly creates a short-lived
        # PyProxy (and may even upload its string representation instead).
        parts.push(getattr(blob, "js_object", blob))
        photo = File.new(parts, filename, to_js({"type": "image/jpeg"}, dict_converter=Object.fromEntries))
        if int(photo.size) <= 0:
            raise RuntimeError("Avatar JPEG is empty")
        form.append("avatar", photo)
        logger.info("Uploading avatar JPEG; bytes=%s, type=%s", photo.size, photo.type)
        result = await self._upload_profile_form(form)
        if result is not True:
            raise RuntimeError("Telegram did not confirm profile photo update")

    async def _upload_profile_form(self, form):
        response = await fetch(
            f"https://api.telegram.org/bot{self.token}/setMyProfilePhoto",
            to_js(
                {
                    "method": "POST",
                    "body": form,
                    "signal": AbortSignal.timeout(25_000),
                },
                dict_converter=Object.fromEntries,
            ),
        )
        return await self._result(response)

    async def send_message(self, chat_id: int, text: str, parse_mode: str | None = "HTML", reply_markup=None):
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return await self.call("sendMessage", payload)

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, parse_mode="HTML", reply_markup=None):
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return await self.call("editMessageText", payload)

    async def answer_callback(self, callback_id: str, text: str = "", show_alert: bool = False):
        payload = {"callback_query_id": callback_id, "show_alert": show_alert}
        if text:
            payload["text"] = text[:200]
        return await self.call("answerCallbackQuery", payload)


async def _refresh_avatar(env, db, settings, bot, force=False):
    async def upload(theme):
        response = await env.ASSETS.fetch(f"https://assets.local/avatars/{theme.filename}")
        if not response.ok:
            raise RuntimeError("Avatar asset is unavailable")
        if response.headers.get("content-type", "").split(";", 1)[0] != "image/jpeg":
            raise RuntimeError("Avatar asset must be a JPEG")
        await bot.set_profile_photo(await response.blob(), theme.filename)

    return await AvatarService(db, settings.timezone, upload).refresh(now_local(settings.timezone), force=force)


async def _runtime(env):
    settings = _settings(env)
    db = Database(Path("/tmp/unused.db"), d1_binding=env.DB)
    await db.init()
    bot = TelegramClient(settings.bot_token)
    calendar = CalendarService(db)
    schedules = ScheduleService(db, settings, calendar)
    sync = SyncService(db, settings)
    notifications = NotificationService(db, settings, schedules)
    return settings, db, bot, schedules, sync, notifications


def _fsm_key(chat_id: int, user_id: int) -> str:
    return json.dumps(
        {"bot": 0, "chat": chat_id, "user": user_id, "thread": None, "business": None, "destiny": "default"},
        separators=(",", ":"),
        sort_keys=True,
    )


async def _get_fsm(env, chat_id: int, user_id: int) -> dict:
    result = (
        await env.DB.prepare("SELECT state,data FROM fsm_state WHERE storage_key=?")
        .bind(_fsm_key(chat_id, user_id))
        .all()
    )
    rows = list(getattr(result, "results", []) or [])
    if not rows:
        return {"state": None, "data": {}}
    row = dict(rows[0])
    try:
        data = json.loads(row.get("data") or "{}")
    except (TypeError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {"state": row.get("state"), "data": data}


async def _set_fsm(env, chat_id: int, user_id: int, state: str | None, data: dict | None = None):
    key = _fsm_key(chat_id, user_id)
    if state is None:
        await env.DB.prepare("DELETE FROM fsm_state WHERE storage_key=?").bind(key).run()
        return
    await (
        env.DB.prepare(
            """INSERT INTO fsm_state(storage_key,state,data,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP)
           ON CONFLICT(storage_key) DO UPDATE SET state=excluded.state,data=excluded.data,updated_at=CURRENT_TIMESTAMP"""
        )
        .bind(key, state, json.dumps(data or {}, ensure_ascii=False))
        .run()
    )


def _is_admin(settings, user_id: int) -> bool:
    return user_id in settings.admin_ids


def _class_display(user) -> str:
    if not user or not user["class_name"]:
        return "класс не выбран"
    profile = f" ({user['profile']})" if user["profile"] else ""
    return f"{user['class_name']}{profile}"


async def _home(user_id, first_name, db, settings, schedules):
    await db.upsert_user(user_id, settings.academic_year, default_reminder=settings.default_reminder_minutes)
    is_admin = _is_admin(settings, user_id)
    user = await db.get_user(user_id)
    if await db.publication_state() == PublicationState.WAITING:
        rows = [
            [_button("🔄 Обновить", "home"), _button("🎓 Мой класс", "class:choose")],
            [_button("⚙️ Настройки", "settings"), _button("❔ Помощь", "help")],
        ]
        if is_admin:
            rows.append([_button("🛠 Админ", "admin")])
        text = render_waiting(await db.waiting_message())
        if user and user["class_name"]:
            text += f"\n\n🎓 Ваш класс: <b>{esc(_class_display(user))}</b>"
        return text, {"inline_keyboard": rows}
    if not user or user["academic_year"] != settings.academic_year or not user["class_name"]:
        picker_text, picker = class_picker(await db.classes())
        picker["inline_keyboard"].append([_button("👥 Друзья", "f:list:0"), _button("⚙️ Настройки", "settings")])
        return f"👋 {esc(first_name)}!\n\n{picker_text}", picker
    now = now_local(settings.timezone)
    current, nxt, info = await schedules.current_and_next(user["class_name"], user["profile"], now)
    parts = [
        f"🏫 <b>Лицей №44</b> · {esc(_class_display(user))}",
        f"📆 {RU_WEEKDAYS[now.weekday()].capitalize()}, {now.strftime('%d.%m · %H:%M')}",
        "",
    ]
    if info["state"] == "day_off":
        parts.append(f"🌿 {esc(info['message'])}")
    elif info["state"] == "no_class_data":
        parts.append("🟠 На сегодня расписание класса ещё не загружено.")
    elif info["state"] == "paused":
        parts.append(f"ℹ️ {esc(info['message'])}")
    else:
        if current:
            parts += ["🟢 <b>Сейчас</b>", lesson_line(current), ""]
            if current.end_time:
                minutes = max(
                    1,
                    int(
                        (datetime.combine(now.date(), current.end_time) - now.replace(tzinfo=None)).total_seconds()
                        // 60
                    ),
                )
                parts += [f"До конца урока: {minutes} мин", ""]
        if nxt:
            parts += ["⏭ <b>Дальше</b>", lesson_line(nxt)]
        elif not current:
            lessons = [lesson for lesson in info.get("lessons", []) if not lesson.cancelled]
            if lessons:
                parts.append(
                    "✅ Занятия на сегодня закончились."
                    if all(lesson.start_time for lesson in lessons)
                    else "📚 Уроки на сегодня — в разделе «Расписание»."
                )
            else:
                parts.append("ℹ️ На сегодня в опубликованном расписании уроков нет.")
        if info.get("bells_known") is False:
            parts += ["", "⚠️ Время звонков пока не настроено; напоминания по времени отключены автоматически."]
    return "\n".join(parts), _main_keyboard(is_admin)


async def _admin_text(db):
    async with db.connect() as connection:
        cursor = await connection.execute("SELECT COUNT(*) AS count FROM users")
        users = int((await cursor.fetchone())["count"])
    sources = await db.list_sources()
    state = await db.publication_state()
    good = sum(1 for src in sources if src["last_status"] == "ok" and src["enabled"])
    bad = sum(1 for src in sources if src["last_status"] in {"error", "stale"} and src["enabled"])
    active = sum(bool(src["enabled"]) for src in sources)
    pending = active - good - bad
    label = {
        PublicationState.WAITING: "Ожидание",
        PublicationState.READY: "Опубликовано",
        PublicationState.PAUSED: "Скрыто",
    }[state]
    return (
        f"🛠 <b>Администрирование</b>\n\n🚦 {label}\n👥 Пользователей: {users}\n"
        f"📡 Активных источников: {active}\n✅ Проверены: {good} · ⏳ Ожидают: {pending} · ❌ Ошибки: {bad}"
    )


async def _queue_broadcast(db, text: str, key: str) -> int:
    queued = 0
    cursor = None
    while True:
        users = await db.all_users(after_user_id=cursor, limit=50)
        if not users:
            return queued
        queued += await db.enqueue_notifications((int(user["user_id"]) for user in users), key, text)
        cursor = int(users[-1]["user_id"])


async def _send_home(bot, chat_id, user_id, first_name, db, settings, schedules, edit_message_id=None):
    text, markup = await _home(user_id, first_name, db, settings, schedules)
    await _panel(bot, chat_id, text, markup, edit_message_id)


async def _answer(bot, callback, text="", show_alert=False):
    if not callback.get("id"):
        return
    try:
        await bot.answer_callback(callback["id"], text, show_alert)
    except Exception:
        logger.debug("Could not answer callback query")


async def _edit(bot, callback, text, markup=None):
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id", callback["from"]["id"])
    return await _panel(bot, chat_id, text, markup, message.get("message_id"))


async def _panel(bot, chat_id, text, markup=None, message_id=None):
    if not message_id:
        return await bot.send_message(chat_id, text, reply_markup=markup)
    try:
        return await bot.edit_message_text(chat_id, message_id, text, reply_markup=markup)
    except Exception as exc:
        error = str(exc).lower()
        if "message is not modified" in error:
            return {"message_id": message_id}
        if "message to edit not found" in error or "message can't be edited" in error:
            return await bot.send_message(chat_id, text, reply_markup=markup)
        raise


async def _student_callback(callback, db, settings, schedules, bot):
    user_id = callback["from"]["id"]
    message = callback.get("message") or {}
    chat_id = message.get("chat", {}).get("id", user_id)
    message_id = message.get("message_id")
    data = callback.get("data", "")
    user = await db.get_user(user_id)
    if not user or user["academic_year"] != settings.academic_year:
        await db.upsert_user(user_id, settings.academic_year, default_reminder=settings.default_reminder_minutes)
        user = await db.get_user(user_id)
    toast = ""
    needs_class = data in {"today", "tomorrow", "changes", "bells"} or data.startswith(("day:", "week:"))
    if needs_class and not user["class_name"]:
        text, keyboard = class_picker(await db.classes())
        await _edit(bot, callback, text, keyboard)
        await _answer(bot, callback, "Выберите класс")
        return
    if data == "home":
        await _send_home(
            bot, chat_id, user_id, callback["from"].get("first_name", "Ученик"), db, settings, schedules, message_id
        )
    elif data in {"today", "tomorrow"} or data.startswith("day:"):
        today = now_local(settings.timezone).date()
        if data == "today":
            day = today
        elif data == "tomorrow":
            day = today + timedelta(days=1)
        else:
            day = date.fromisoformat(data.split(":", 1)[1])
        if abs((day - today).days) > 56:
            await _answer(bot, callback, "Доступны даты в пределах восьми недель", True)
            return
        info = await schedules.day_schedule(user["class_name"], user["profile"], day)
        await _edit(bot, callback, render_day(day, _class_display(user), info), day_markup(day, today))
    elif data.startswith("week:"):
        if await db.publication_state() == PublicationState.WAITING:
            await _edit(
                bot, callback, render_waiting(await db.waiting_message()), _kb([_button("← На главную", "home")])
            )
        else:
            offset = max(-8, min(8, int(data.split(":", 1)[1])))
            today = now_local(settings.timezone).date()
            monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
            days = [
                await schedules.day_schedule(user["class_name"], user["profile"], monday + timedelta(days=i))
                for i in range(7)
            ]
            await _edit(bot, callback, render_week(monday, _class_display(user), days), week_markup(monday, offset))
    elif data == "changes":
        if await db.publication_state() == PublicationState.WAITING:
            await _edit(
                bot, callback, render_waiting(await db.waiting_message()), _kb([_button("← На главную", "home")])
            )
        else:
            today = now_local(settings.timezone).date()
            found = []
            async with db.connect() as connection:
                cursor = await connection.execute(
                    """SELECT DISTINCT r.lesson_date FROM replacements r JOIN data_sources ds ON ds.id=r.source_id
                       WHERE ds.enabled=1 AND r.class_name=? AND r.lesson_date>=? AND r.lesson_date<?
                       ORDER BY r.lesson_date""",
                    (user["class_name"], today.isoformat(), (today + timedelta(days=7)).isoformat()),
                )
                dates = [date.fromisoformat(row["lesson_date"]) for row in await cursor.fetchall()]
            for day in dates:
                info = await schedules.day_schedule(user["class_name"], user["profile"], day)
                changed = [lesson for lesson in info.get("lessons", []) if lesson.changed]
                if changed:
                    found.append((day, changed))
            lines = [
                f"⚡ <b>Список замен</b> · {esc(_class_display(user))}",
                f"{today:%d.%m}–{today + timedelta(days=6):%d.%m}",
            ]
            for day, lessons in found:
                lines += ["", f"<b>{day:%d.%m}</b>", *[lesson_line(lesson) for lesson in lessons]]
            if not found:
                lines += ["", "На ближайшие 7 дней опубликованных изменений нет."]
            await _edit(
                bot,
                callback,
                fit_lines(lines),
                _kb(
                    [_button("🔄 Обновить замены", "changes"), _button("📆 Неделя", "week:0")],
                    [_button("← Меню", "home")],
                ),
            )
    elif data == "class:choose" or data.startswith("grade:"):
        grade = data.split(":", 1)[1] if data.startswith("grade:") else None
        text, keyboard = class_picker(await db.classes(), grade, user["class_name"])
        await _edit(bot, callback, text, keyboard)
    elif data.startswith("classn:"):
        class_name = data.split(":", 1)[1]
        classes = await db.classes()
        if class_name not in {name for name, _ in classes}:
            await _answer(bot, callback, "Класс больше недоступен. Обновите список.", True)
            return
        profiles = sorted({profile for name, profile in classes if name == class_name and profile})
        if not profiles:
            await db.set_user_class(
                user_id, class_name, None, settings.academic_year, settings.default_reminder_minutes
            )
            await _send_home(
                bot, chat_id, user_id, callback["from"].get("first_name", "Ученик"), db, settings, schedules, message_id
            )
            toast = f"Выбран {class_name.upper()}"
        else:
            rows = [[_button(profile, f"profile:{class_name}:{index}")] for index, profile in enumerate(profiles)]
            rows.append([_button("← Назад", "class:choose")])
            await _edit(
                bot, callback, f"Выберите профиль для <b>{esc(class_name.upper())}</b>", {"inline_keyboard": rows}
            )
    elif data.startswith("profile:"):
        _, class_name, index_s = data.split(":", 2)
        profiles = sorted({profile for name, profile in await db.classes() if name == class_name and profile})
        index = int(index_s)
        if index < 0 or index >= len(profiles):
            await _answer(bot, callback, "Список изменился, выберите снова", True)
            return
        await db.set_user_class(
            user_id, class_name, profiles[index], settings.academic_year, settings.default_reminder_minutes
        )
        await _send_home(
            bot, chat_id, user_id, callback["from"].get("first_name", "Ученик"), db, settings, schedules, message_id
        )
        toast = "Класс и профиль сохранены"
    elif data in {"settings", "settings:notifications"} or data.startswith(("setting:", "remind:")):
        if data.startswith("setting:"):
            field = data.split(":", 1)[1]
            if field not in {"notifications", "replacement_notifications", "morning_summary"}:
                await _answer(bot, callback, "Неизвестная настройка", True)
                return
            await db.update_user_setting(user_id, field, 0 if user[field] else 1)
        elif data.startswith("remind:"):
            minutes = int(data.split(":", 1)[1])
            if minutes not in (5, 10, 15, 20, 30, 45):
                await _answer(bot, callback, "Недопустимый интервал", True)
                return
            await db.update_user_setting(user_id, "reminder_minutes", minutes)
            toast = f"Напоминать за {minutes} мин"
        user = await db.get_user(user_id)
        text, keyboard = (
            settings_view(user, _class_display(user), _is_admin(settings, user_id))
            if data == "settings"
            else notification_settings_view(user, _class_display(user))
        )
        await _edit(bot, callback, text, keyboard)
    elif data == "bells":
        bells = schedules.bell_times_for_class(user["class_name"])
        await _edit(
            bot,
            callback,
            bells_text(bells, _class_display(user)),
            _kb([_button("📅 Сегодня", "today"), _button("← Меню", "home")]),
        )
    elif data == "help":
        await _edit(
            bot,
            callback,
            HELP_TEXT,
            _kb([_button("🎓 Выбрать класс", "class:choose"), _button("← Меню", "home")]),
        )
    else:
        await _answer(bot, callback)
        return
    await _answer(bot, callback, toast)


async def _admin_callback(callback, db, settings, bot, sync, env):
    user_id = callback["from"]["id"]
    message = callback.get("message") or {}
    chat_id = message.get("chat", {}).get("id", user_id)
    message_id = message.get("message_id")
    data = callback.get("data", "")
    if not _is_admin(settings, user_id):
        await _answer(bot, callback, "Нет доступа", True)
        return
    if data in {"admin", "admin:sources"}:
        await _set_fsm(env, chat_id, user_id, None)
    if data == "admin":
        await _edit(bot, callback, await _admin_text(db), _admin_keyboard())
    elif data == "admin:sources":
        sources = await db.list_sources()
        lines = ["📡 <b>Источники данных</b>", ""]
        rows = []
        for source in sources:
            icon = {"ok": "✅", "error": "❌", "stale": "🟡"}.get(source["last_status"], "⚪")
            lines.append(
                f"{'🟢' if source['enabled'] else '⏸'}{icon} #{source['id']} · {esc(source['name'])} · <code>{source['source_type']}</code>"
            )
            rows.append([_button(f"#{source['id']} {source['name'][:28]}", f"admin:source:{source['id']}")])
        if not sources:
            lines.append("Источников пока нет.")
        rows += [
            [_button("➕ Основное расписание", "admin:add:weekly")],
            [_button("➕ Расписание на дату", "admin:add:date")],
            [_button("➕ Замены", "admin:add:replacements")],
            [_button("← Админ", "admin")],
        ]
        await _edit(bot, callback, "\n".join(lines)[:4096], {"inline_keyboard": rows})
    elif data.startswith("admin:source:"):
        sid = int(data.rsplit(":", 1)[1])
        source = await db.get_source(sid)
        if not source:
            await _answer(bot, callback, "Источник уже удалён", True)
            return
        text = (
            f"📡 <b>{esc(source['name'])}</b>\n\nID: {sid}\nТип: <code>{source['source_type']}</code>\n"
            f"Статус: {esc(source['last_status'])}\nАктивен: {'да' if source['enabled'] else 'нет'}\n"
            f"Приоритет: {source['priority']}\nПериод: {source['valid_from'] or '—'} → {source['valid_until'] or '—'}\n"
            f"Ошибка: {esc(source['last_error'] or '—')}\n\n<code>{esc(source['url'])}</code>"
        )
        await _edit(
            bot,
            callback,
            text,
            _kb(
                [_button("🔄 Проверить сейчас", f"admin:source_sync:{sid}")],
                [_button("⏸ Отключить" if source["enabled"] else "▶️ Включить", f"admin:source_toggle:{sid}")],
                [_button("🗑 Удалить", f"admin:source_delete_confirm:{sid}")],
                [_button("← Источники", "admin:sources")],
            ),
        )
    elif data.startswith("admin:source_sync:"):
        sid = int(data.rsplit(":", 1)[1])
        await _answer(bot, callback, "Проверяю…")
        try:
            result = await sync.sync_source(sid)
            text = f"✅ Источник проверен, данные обновлены. Записей: <b>{result['count']}</b>"
        except Exception as exc:
            text = f"❌ Источник не принят; рабочие данные не изменены.\n\n<code>{esc(exc)}</code>"
        await _edit(bot, callback, text, _kb([_button("← К источнику", f"admin:source:{sid}")]))
        return
    elif data.startswith("admin:source_toggle:"):
        sid = int(data.rsplit(":", 1)[1])
        source = await db.get_source(sid)
        if not source:
            await _answer(bot, callback, "Источник уже удалён", True)
            return
        await db.set_source_enabled(sid, not bool(source["enabled"]), user_id)
        callback["data"] = f"admin:source:{sid}"
        await _admin_callback(callback, db, settings, bot, sync, env)
        return
    elif data.startswith("admin:source_delete_confirm:"):
        sid = int(data.rsplit(":", 1)[1])
        await _edit(
            bot,
            callback,
            "Удалить источник и его snapshot? Это не затронет другие источники.",
            _kb([_button("🗑 Да, удалить", f"admin:source_delete:{sid}"), _button("Отмена", f"admin:source:{sid}")]),
        )
    elif data.startswith("admin:source_delete:"):
        sid = int(data.rsplit(":", 1)[1])
        await db.delete_source(sid, user_id)
        callback["data"] = "admin:sources"
        await _admin_callback(callback, db, settings, bot, sync, env)
        return
    elif data.startswith("admin:add:"):
        kind = data.rsplit(":", 1)[1]
        source_type = {
            "weekly": SourceType.WEEKLY.value,
            "date": SourceType.DATE.value,
            "replacements": SourceType.REPLACEMENTS.value,
        }.get(kind)
        if source_type is None:
            await _answer(bot, callback, "Неизвестный тип источника", True)
            return
        fsm_state = "source_date" if source_type == SourceType.DATE.value else "source_url"
        await _set_fsm(env, chat_id, user_id, fsm_state, {"source_type": source_type, "panel_message_id": message_id})
        prompt = (
            "📅 Введите дату в формате <code>02.09.2026</code>."
            if fsm_state == "source_date"
            else "🔗 Пришлите ссылку на нужную вкладку Google Sheets с <code>gid=…</code>.\n\nБольшие книги добавляйте по отдельным вкладкам."
        )
        await _edit(bot, callback, prompt, _kb([_button("Отмена", "admin:sources")]))
    elif data == "admin:add_confirm":
        fsm = await _get_fsm(env, chat_id, user_id)
        values = fsm["data"]
        if not values.get("url"):
            await _answer(bot, callback, "Сессия добавления устарела", True)
            return
        source_type = SourceType(values["source_type"])
        valid = date.fromisoformat(values["valid_date"]) if values.get("valid_date") else None
        label = (
            "Основное"
            if source_type == SourceType.WEEKLY
            else "Замены"
            if source_type == SourceType.REPLACEMENTS
            else f"День {valid:%d.%m}"
        )
        sid = await db.add_source(
            f"{label} · {datetime.now():%d.%m %H:%M:%S}",
            source_type,
            values["url"],
            500 if source_type == SourceType.DATE else 100,
            valid,
            valid if source_type == SourceType.DATE else None,
            settings.academic_year,
            user_id,
        )
        try:
            result = await sync.sync_source(sid)
        except Exception as exc:
            await db.delete_source(sid, user_id)
            await _edit(bot, callback, f"❌ Источник не сохранён: <code>{esc(exc)}</code>", _admin_keyboard())
            await _set_fsm(env, chat_id, user_id, None)
            return
        await _set_fsm(env, chat_id, user_id, None)
        await _edit(
            bot,
            callback,
            f"✅ Источник сохранён. Записей: {result['count']}. Ученики увидят его после публикации.",
            _kb([_button("🚦 Публикация", "admin:publication")], [_button("📡 Источники", "admin:sources")]),
        )
    elif data == "admin:sync":
        await _answer(bot, callback, "Синхронизирую один источник…")
        result = await sync.sync_next()
        await _edit(
            bot,
            callback,
            f"🔄 Проверка источника: ✅ {result['ok']} · ❌ {result['failed']} · ⏭ {result['skipped']}\nОстальные проверяются автоматически по очереди.",
            _kb(
                [_button("🔄 Проверить следующий", "admin:sync"), _button("📡 Источники", "admin:sources")],
                [_button("← Админ", "admin")],
            ),
        )
        return
    elif data == "admin:publication":
        sources = await db.list_sources(True)
        good = sum(
            1
            for source in sources
            if source["last_status"] == "ok"
            and source["source_type"] in (SourceType.WEEKLY.value, SourceType.DATE.value)
        )
        text = f"🚦 <b>Публикация</b>\n\nТекущее состояние: <b>{(await db.publication_state()).value}</b>\nПроверенных источников расписания: <b>{good}</b>"
        await _edit(
            bot,
            callback,
            text,
            _kb(
                [_button("✅ Опубликовать", "admin:publish")],
                [_button("🟡 Вернуть в ожидание", "admin:set_waiting")],
                [_button("📣 Сообщить об ожидании", "admin:notify_waiting")],
                [_button("← Админ", "admin")],
            ),
        )
    elif data == "admin:publish":
        sources = await db.list_sources(True)
        if not any(
            source["last_status"] == "ok" and source["source_type"] in (SourceType.WEEKLY.value, SourceType.DATE.value)
            for source in sources
        ):
            await _answer(bot, callback, "Нет проверенного источника расписания", True)
            return
        await db.set_publication_state(PublicationState.READY, user_id)
        await _edit(
            bot,
            callback,
            "✅ Расписание опубликовано. Дополнительное сообщение ученикам не отправлялось.",
            _admin_keyboard(),
        )
    elif data == "admin:set_waiting":
        await db.set_publication_state(PublicationState.WAITING, user_id)
        await _edit(bot, callback, "🟡 Режим ожидания включён.", _admin_keyboard())
    elif data == "admin:notify_waiting":
        text = f"🟡 <b>Расписание ещё формируется</b>\n\n{esc(await db.waiting_message())}"
        key = f"broadcast:waiting:{callback['id']}"
        count = await _queue_broadcast(db, text, key)
        await _edit(
            bot,
            callback,
            f"📬 В очередь уведомлений добавлено: {count}. Сообщения будут отправляться постепенно, с повторами при временных ошибках.",
            _admin_keyboard(),
        )
    elif data == "admin:waiting_message":
        await _set_fsm(env, chat_id, user_id, "waiting_message", {"panel_message_id": message_id})
        await _edit(
            bot,
            callback,
            f"✏️ Текущее сообщение:\n\n{esc(await db.waiting_message())}\n\nПришлите новый текст (10–1000 символов).",
            _kb([_button("Отмена", "admin")]),
        )
    elif data == "admin:broadcast":
        await _set_fsm(env, chat_id, user_id, "broadcast_text", {"panel_message_id": message_id})
        await _edit(
            bot,
            callback,
            "📣 Пришлите текст рассылки (до 3000 символов).\nПеред отправкой будет предпросмотр.",
            _kb([_button("Отмена", "admin")]),
        )
    elif data == "admin:broadcast_confirm":
        fsm = await _get_fsm(env, chat_id, user_id)
        text = fsm["data"].get("broadcast")
        if not text:
            await _answer(bot, callback, "Черновик устарел", True)
            return
        count = await _queue_broadcast(db, esc(text), f"broadcast:{callback['id']}")
        await _set_fsm(env, chat_id, user_id, None)
        await _edit(
            bot,
            callback,
            f"📬 В очередь уведомлений добавлено: {count}. Рассылка будет постепенно доставлена; временные ошибки повторяются автоматически.",
            _admin_keyboard(),
        )
    else:
        await _answer(bot, callback)
        return
    await _answer(bot, callback)


async def _flow_panel(env, bot, chat_id, user_id, state, data, text, keyboard):
    result = await _panel(bot, chat_id, text, keyboard, data.get("panel_message_id"))
    if result and result.get("message_id"):
        data["panel_message_id"] = result["message_id"]
    await _set_fsm(env, chat_id, user_id, state, data)


async def _handle_message(message, env, settings, db, bot, schedules, sync):
    sender = message.get("from") or {}
    user_id = sender.get("id")
    chat_id = (message.get("chat") or {}).get("id")
    if not user_id or not chat_id:
        return
    text = (message.get("text") or "").strip()
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""
    if command in {"/start", "/menu", "/cancel"}:
        panel_id = None
        if command == "/cancel":
            panel_id = (await _get_fsm(env, chat_id, user_id))["data"].get("panel_message_id")
        await _set_fsm(env, chat_id, user_id, None)
        await _send_home(bot, chat_id, user_id, sender.get("first_name", "Ученик"), db, settings, schedules, panel_id)
        return
    if command == "/friends":
        if chat_id != user_id:
            await bot.send_message(chat_id, "Откройте список друзей в личном чате с ботом.")
            return
        await db.upsert_user(user_id, settings.academic_year, default_reminder=settings.default_reminder_minutes)
        view = await FriendsService(db, settings, schedules).page(user_id, 0, now_local(settings.timezone))
        await _flow_panel(env, bot, chat_id, user_id, view[2], view[3], view[0], view[1])
        return
    if command in {"/today", "/tomorrow", "/week", "/changes", "/settings", "/help", "/bells"}:
        await _set_fsm(env, chat_id, user_id, None)
        await _student_callback(
            {
                "from": sender,
                "message": {"chat": {"id": chat_id}},
                "data": "week:0" if command == "/week" else command[1:],
            },
            db,
            settings,
            schedules,
            bot,
        )
        return
    if command == "/id":
        await bot.send_message(chat_id, f"Ваш Telegram ID: <code>{user_id}</code>")
        return
    if command == "/admin":
        if _is_admin(settings, user_id):
            await _set_fsm(env, chat_id, user_id, None)
            await bot.send_message(chat_id, await _admin_text(db), reply_markup=_admin_keyboard())
        return
    if command in {"/dayoff", "/schoolday", "/dayclear"}:
        if not _is_admin(settings, user_id):
            return
        parts = text.split(maxsplit=3)
        command = parts[0].split("@")[0]
        if len(parts) < 2:
            await bot.send_message(
                chat_id,
                "Использование: /dayoff YYYY-MM-DD [причина] · /schoolday YYYY-MM-DD [0-6] [причина] · /dayclear YYYY-MM-DD",
            )
            return
        try:
            day = date.fromisoformat(parts[1])
        except ValueError:
            await bot.send_message(chat_id, "Некорректная дата.")
            return
        if command == "/dayoff":
            await db.set_calendar_day(
                day, False, None, " ".join(parts[2:]) if len(parts) > 2 else "Неучебный день", user_id
            )
            response = f"✅ {day:%d.%m.%Y} отмечен как неучебный день."
        elif command == "/schoolday":
            if len(parts) > 2 and (not parts[2].isdigit() or int(parts[2]) not in range(7)):
                await bot.send_message(chat_id, "День недели: от 0 (понедельник) до 6 (воскресенье).")
                return
            weekday = int(parts[2]) if len(parts) > 2 else day.weekday()
            note = parts[3] if len(parts) > 3 else "Учебный день"
            await db.set_calendar_day(day, True, weekday, note, user_id)
            response = f"✅ {day:%d.%m.%Y} отмечен как учебный день."
        else:
            await db.clear_calendar_day(day)
            response = "✅ Исключение календаря удалено."
        await bot.send_message(chat_id, response)
        return
    if command:
        await bot.send_message(chat_id, "Неизвестная команда. Откройте /help или /start.")
        return

    fsm = await _get_fsm(env, chat_id, user_id)
    state, data = fsm["state"], fsm["data"]
    if state == "friend:name":
        try:
            view = await FriendsService(db, settings, schedules).message(
                user_id, text, data, now_local(settings.timezone)
            )
        except ValueError as exc:
            await _flow_panel(env, bot, chat_id, user_id, state, data, esc(exc), _kb([_button("Отмена", "f:manage:0")]))
            return
        view[3]["panel_message_id"] = data.get("panel_message_id")
        await _flow_panel(env, bot, chat_id, user_id, view[2], view[3], view[0], view[1])
        return
    if not _is_admin(settings, user_id):
        return
    cancel = _kb([_button("Отмена", "admin")])
    if state == "source_date":
        try:
            valid = datetime.strptime(text, "%d.%m.%Y").date()
        except ValueError:
            await _flow_panel(
                env,
                bot,
                chat_id,
                user_id,
                state,
                data,
                "Не понял дату. Пример: <code>02.09.2026</code>\nПришлите дату ещё раз.",
                cancel,
            )
            return
        data["valid_date"] = valid.isoformat()
        await _flow_panel(
            env,
            bot,
            chat_id,
            user_id,
            "source_url",
            data,
            "🔗 Теперь пришлите ссылку на вкладку Google Sheets с однодневным расписанием (<code>gid=…</code>).",
            cancel,
        )
    elif state == "source_url":
        data.pop("url", None)
        try:
            _, gid = google_sheet_parts(text)
            if gid is None:
                raise ValueError(
                    "Нужна ссылка на отдельную вкладку с gid=…: откройте вкладку в браузере и скопируйте её адрес."
                )
            preview = await sync.preview(
                SourceType(data["source_type"]),
                text,
                date.fromisoformat(data["valid_date"]) if data.get("valid_date") else None,
                settings.academic_year,
            )
        except Exception as exc:
            await _flow_panel(
                env,
                bot,
                chat_id,
                user_id,
                state,
                data,
                f"❌ Не удалось проверить таблицу:\n{esc(exc)}\n\nПришлите другую ссылку или отмените добавление.",
                cancel,
            )
            return
        data["url"] = text
        date_line = f"\nДата: {preview['date']:%d.%m.%Y}" if preview.get("date") else ""
        classes = ", ".join(name.upper() for name in preview.get("classes", [])) or "—"
        summary = f"✅ <b>Источник распознан</b>\n\nТип: <code>{data['source_type']}</code>\nЗаписей: {preview['count']}\nКлассы: {esc(classes)}\nУчебный год: {esc(preview.get('declared_year') or 'не указан')}{date_line}\n\nСохранить?"
        await _flow_panel(
            env,
            bot,
            chat_id,
            user_id,
            "source_url",
            data,
            summary,
            _kb([_button("✅ Сохранить", "admin:add_confirm"), _button("Отмена", "admin:sources")]),
        )
    elif state == "waiting_message":
        if not 10 <= len(text) <= 1000:
            await _flow_panel(
                env,
                bot,
                chat_id,
                user_id,
                state,
                data,
                "Текст должен быть длиной 10–1000 символов. Пришлите его ещё раз.",
                cancel,
            )
            return
        await db.set_waiting_message(text, user_id)
        await _flow_panel(env, bot, chat_id, user_id, None, data, "✅ Сообщение ожидания обновлено.", _admin_keyboard())
    elif state == "broadcast_text":
        if not text or len(text) > 3000 or len(esc(text)) > 3800:
            await _flow_panel(
                env,
                bot,
                chat_id,
                user_id,
                state,
                data,
                "Текст слишком длинный или пустой. Пришлите более короткое сообщение для рассылки.",
                cancel,
            )
            return
        data["broadcast"] = text
        await _flow_panel(
            env,
            bot,
            chat_id,
            user_id,
            "broadcast_text",
            data,
            "<b>Предпросмотр:</b>\n\n" + esc(text),
            _kb([_button("📣 Отправить всем", "admin:broadcast_confirm"), _button("Отмена", "admin")]),
        )


async def _process_update(update, env):
    settings, db, bot, schedules, sync, _ = await _runtime(env)
    callback = update.get("callback_query")
    if callback:
        data = callback.get("data", "")
        try:
            if data.startswith("f:"):
                user_id = callback["from"]["id"]
                chat_id = ((callback.get("message") or {}).get("chat") or {}).get("id", user_id)
                if chat_id != user_id:
                    await _answer(bot, callback, "Откройте друзей в личном чате с ботом.", True)
                    return
                await db.upsert_user(
                    user_id, settings.academic_year, default_reminder=settings.default_reminder_minutes
                )
                fsm = await _get_fsm(env, chat_id, user_id)
                try:
                    view = await FriendsService(db, settings, schedules).callback(
                        user_id, data, fsm["state"], fsm["data"], now_local(settings.timezone)
                    )
                except ValueError as exc:
                    await _answer(bot, callback, str(exc), True)
                    return
                view[3]["panel_message_id"] = (callback.get("message") or {}).get("message_id")
                await _flow_panel(env, bot, chat_id, user_id, view[2], view[3], view[0], view[1])
                await _answer(bot, callback, view[4])
            elif data.startswith("admin"):
                await _admin_callback(callback, db, settings, bot, sync, env)
            else:
                chat_id = ((callback.get("message") or {}).get("chat") or {}).get("id", callback["from"]["id"])
                await _set_fsm(env, chat_id, callback["from"]["id"], None)
                await _student_callback(callback, db, settings, schedules, bot)
        except ValueError:
            await _answer(bot, callback, "Кнопка устарела. Откройте /start.", True)
    else:
        message = update.get("message") or update.get("edited_message")
        if message:
            await _handle_message(message, env, settings, db, bot, schedules, sync)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        path = request.url.split("?", 1)[0]
        if request.method == "GET" and path.endswith("/health"):
            try:
                db = Database(Path("/tmp/unused.db"), d1_binding=self.env.DB)
                async with db.connect() as connection:
                    await connection.execute("SELECT value FROM app_state LIMIT 1")
                return Response("ok", headers={"content-type": "text/plain"})
            except Exception:
                logger.exception("Health check failed")
                return Response("unhealthy", status=503, headers={"content-type": "text/plain"})
        if request.method != "POST" or not path.endswith("/telegram"):
            return Response("not found", status=404)
        expected = _value(self.env, "TELEGRAM_WEBHOOK_SECRET")
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not expected or supplied != expected:
            return Response("unauthorized", status=401)
        try:
            payload = await request.json()
            if len(json.dumps(payload)) > 1_000_000:
                return Response("payload too large", status=413)
            await _process_update(payload, self.env)
            return Response("ok")
        except Exception:
            logger.exception("Telegram webhook update failed")
            return Response("internal error", status=500)

    async def scheduled(self, controller, env, ctx):
        # In Workers Python, bindings are reliably available on WorkerEntrypoint.env
        # for both fetch and scheduled events. The scheduled callback's `env`
        # argument is not populated consistently by local/production runtimes.
        env = self.env
        settings, db, bot, schedules, sync, notifications = await _runtime(env)
        try:
            if controller.cron == "*/2 * * * *":
                await sync.bootstrap()
                await sync.sync_next()
            elif controller.cron == "1-59/2 * * * *":
                if await db.get_state("avatar_manual_pending", "0") == "1":
                    # One explicit operator request replaces only one tick,
                    # never shares its CPU budget with notification rendering.
                    # Consume before upload so failures cannot stall notices.
                    await db.set_state("avatar_manual_pending", "0")
                    changed = await _refresh_avatar(env, db, settings, bot, force=True)
                    logger.info("Manual avatar rotation checked; changed=%s", changed)
                else:
                    await notifications.tick(bot, user_limit=2)
            elif controller.cron == AVATAR_CRON:
                changed = await _refresh_avatar(env, db, settings, bot)
                logger.info("Avatar rotation checked; changed=%s", changed)
        except Exception:
            logger.exception("Scheduled task failed")
