from __future__ import annotations

from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..config import Settings
from ..db import Database
from ..domain import PublicationState
from ..services.schedule import ScheduleService
from ..ui import back_home, lesson_line, main_keyboard, render_day, render_waiting
from ..utils import RU_WEEKDAYS, esc, now_local

router = Router()


def _is_admin(settings: Settings, user_id: int) -> bool:
    return user_id in settings.admin_ids


def _class_display(row) -> str:
    if not row or not row["class_name"]:
        return "класс не выбран"
    profile = f" ({row['profile']})" if row["profile"] else ""
    return f"{row['class_name']}{profile}"


async def render_home(
    user_id: int, first_name: str, db: Database, settings: Settings, schedules: ScheduleService
) -> tuple[str, InlineKeyboardMarkup]:
    await db.upsert_user(user_id, settings.academic_year, default_reminder=settings.default_reminder_minutes)
    state = await db.publication_state()
    if state == PublicationState.WAITING:
        return render_waiting(await db.waiting_message()), main_keyboard(_is_admin(settings, user_id))
    user = await db.get_user(user_id)
    if not user or user["academic_year"] != settings.academic_year or not user["class_name"]:
        text = (
            f"👋 <b>{esc(first_name)}</b>\n\n"
            "✅ Расписание опубликовано.\n"
            "Чтобы показывать только ваши уроки и присылать правильные уведомления, выберите актуальный класс."
        )
        return text, main_keyboard(_is_admin(settings, user_id), class_selected=False)
    now = now_local(settings.timezone)
    current, nxt, info = await schedules.current_and_next(user["class_name"], user["profile"], now)
    class_display = _class_display(user)
    parts = [
        f"🏫 <b>Лицей №44</b> · {esc(class_display)}",
        f"📆 {RU_WEEKDAYS[now.weekday()].capitalize()}, {now.strftime('%d.%m · %H:%M')}",
        "",
    ]
    if info["state"] == "day_off":
        parts.append(f"🌿 {esc(info['message'])}")
    elif info["state"] == "no_class_data":
        parts.append("🟠 <b>На сегодня данные для вашего класса ещё не загружены.</b>")
        parts.append("Это не означает, что уроков нет. Проверьте позже или дождитесь обновления от администрации.")
    elif info["state"] == "paused":
        parts.append(f"ℹ️ {esc(info['message'])}")
    else:
        if current:
            parts += ["🟢 <b>Сейчас</b>", lesson_line(current), ""]
        if nxt:
            parts += ["⏭ <b>Дальше</b>", lesson_line(nxt)]
        elif not current:
            lessons = [x for x in info.get("lessons", []) if not x.cancelled]
            if lessons:
                future_without_time = [x for x in lessons if x.start_time is None]
                if future_without_time:
                    parts += [
                        "📚 <b>Расписание на сегодня загружено</b>",
                        "Откройте «Сегодня», чтобы увидеть все уроки.",
                    ]
                else:
                    parts.append("✅ На сегодня занятия закончились.")
            else:
                parts.append("ℹ️ На сегодня в опубликованном расписании уроков нет.")
        if info.get("bells_known") is False:
            parts += ["", "⚠️ Время звонков пока не настроено; напоминания по времени отключены автоматически."]
    return "\n".join(parts), main_keyboard(_is_admin(settings, user_id))


@router.message(CommandStart())
async def start(message: Message, db: Database, settings: Settings, schedules: ScheduleService):
    text, kb = await render_home(
        message.from_user.id, message.from_user.first_name or "Ученик", db, settings, schedules
    )
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(Command("id"))
async def user_id(message: Message):
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


@router.callback_query(F.data == "home")
async def home(callback: CallbackQuery, db: Database, settings: Settings, schedules: ScheduleService):
    text, kb = await render_home(
        callback.from_user.id, callback.from_user.first_name or "Ученик", db, settings, schedules
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "today")
async def today(callback: CallbackQuery, db: Database, settings: Settings, schedules: ScheduleService):
    user = await db.get_user(callback.from_user.id)
    state = await db.publication_state()
    if state == PublicationState.WAITING:
        await callback.message.edit_text(
            render_waiting(await db.waiting_message()), parse_mode="HTML", reply_markup=back_home()
        )
    elif not user or not user["class_name"] or user["academic_year"] != settings.academic_year:
        await callback.message.edit_text(
            "Сначала выберите актуальный класс.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🎓 Выбрать класс", callback_data="class:choose")],
                    [InlineKeyboardButton(text="← На главную", callback_data="home")],
                ]
            ),
        )
    else:
        day = now_local(settings.timezone).date()
        info = await schedules.day_schedule(user["class_name"], user["profile"], day)
        await callback.message.edit_text(
            render_day(day, _class_display(user), info), parse_mode="HTML", reply_markup=back_home()
        )
    await callback.answer()


@router.callback_query(F.data.startswith("week:"))
async def week(callback: CallbackQuery, db: Database, settings: Settings, schedules: ScheduleService):
    user = await db.get_user(callback.from_user.id)
    if await db.publication_state() == PublicationState.WAITING:
        await callback.message.edit_text(
            render_waiting(await db.waiting_message()), parse_mode="HTML", reply_markup=back_home()
        )
        await callback.answer()
        return
    if not user or not user["class_name"] or user["academic_year"] != settings.academic_year:
        await callback.answer("Сначала выберите класс", show_alert=True)
        return
    offset = max(-8, min(8, int(callback.data.split(":", 1)[1])))
    today = now_local(settings.timezone).date()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    parts = [
        f"📆 <b>Неделя {monday.strftime('%d.%m')}–{(monday + timedelta(days=6)).strftime('%d.%m')}</b> · {esc(_class_display(user))}"
    ]
    for i in range(5):
        day = monday + timedelta(days=i)
        info = await schedules.day_schedule(user["class_name"], user["profile"], day)
        parts += ["", f"<b>{RU_WEEKDAYS[i].capitalize()} · {day.strftime('%d.%m')}</b>"]
        if info["state"] == "ready":
            parts += [lesson_line(x) for x in info["lessons"]]
        else:
            parts.append(f"ℹ️ {esc(info['message'])}")
    text = "\n".join(parts)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="←", callback_data=f"week:{offset - 1}"),
                InlineKeyboardButton(text="Текущая", callback_data="week:0"),
                InlineKeyboardButton(text="→", callback_data=f"week:{offset + 1}"),
            ],
            [InlineKeyboardButton(text="← На главную", callback_data="home")],
        ]
    )
    await callback.message.edit_text(text[:4096], parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "changes")
async def changes(callback: CallbackQuery, db: Database, settings: Settings, schedules: ScheduleService):
    user = await db.get_user(callback.from_user.id)
    if await db.publication_state() == PublicationState.WAITING:
        await callback.message.edit_text(
            render_waiting(await db.waiting_message()), parse_mode="HTML", reply_markup=back_home()
        )
        await callback.answer()
        return
    if not user or not user["class_name"] or user["academic_year"] != settings.academic_year:
        await callback.answer("Сначала выберите класс", show_alert=True)
        return
    today = now_local(settings.timezone).date()
    found = []
    for delta in range(14):
        day = today + timedelta(days=delta)
        info = await schedules.day_schedule(user["class_name"], user["profile"], day)
        changed = [x for x in info.get("lessons", []) if x.changed]
        if changed:
            found.append((day, changed))
    parts = ["⚡ <b>Список замен для вашего класса</b>"]
    if not found:
        parts += ["", "На ближайшие 14 дней опубликованных изменений нет."]
    for day, lessons in found:
        parts += ["", f"<b>{day.strftime('%d.%m')}</b>", *[lesson_line(x) for x in lessons]]
    await callback.message.edit_text("\n".join(parts)[:4096], parse_mode="HTML", reply_markup=back_home())
    await callback.answer()


@router.callback_query(F.data == "class:choose")
async def choose_class(callback: CallbackQuery, db: Database, settings: Settings):
    if await db.publication_state() == PublicationState.WAITING:
        await callback.answer("Классы появятся после публикации расписания", show_alert=True)
        return
    classes = await db.classes()
    names = sorted({c for c, _ in classes}, key=lambda x: (int("".join(filter(str.isdigit, x)) or 0), x))
    if not names:
        await callback.answer("В расписании пока не найдено ни одного класса", show_alert=True)
        return
    rows = [
        [InlineKeyboardButton(text=name.upper(), callback_data=f"classn:{name}") for name in names[i : i + 4]]
        for i in range(0, len(names), 4)
    ]
    rows.append([InlineKeyboardButton(text="← На главную", callback_data="home")])
    await callback.message.edit_text(
        "🎓 <b>Выберите класс</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("classn:"))
async def choose_profile(callback: CallbackQuery, db: Database, settings: Settings):
    class_name = callback.data.split(":", 1)[1]
    choices = [(c, p) for c, p in await db.classes() if c == class_name]
    profiles = sorted({p for _, p in choices if p})
    if not profiles:
        await db.set_user_class(
            callback.from_user.id, class_name, None, settings.academic_year, settings.default_reminder_minutes
        )
        await callback.answer("Класс сохранён")
        await callback.message.edit_text("✅ Класс сохранён.", reply_markup=back_home())
        return
    rows = [[InlineKeyboardButton(text=p, callback_data=f"profile:{class_name}:{i}")] for i, p in enumerate(profiles)]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="class:choose")])
    await callback.message.edit_text(
        f"Выберите профиль для <b>{esc(class_name.upper())}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("profile:"))
async def set_profile(callback: CallbackQuery, db: Database, settings: Settings):
    _, class_name, idx_s = callback.data.split(":", 2)
    profiles = sorted({p for c, p in await db.classes() if c == class_name and p})
    idx = int(idx_s)
    if idx >= len(profiles):
        await callback.answer("Список изменился, выберите снова", show_alert=True)
        return
    await db.set_user_class(
        callback.from_user.id, class_name, profiles[idx], settings.academic_year, settings.default_reminder_minutes
    )
    await callback.message.edit_text("✅ Класс и профиль сохранены.", reply_markup=back_home())
    await callback.answer()


@router.callback_query(F.data == "settings")
async def settings_screen(callback: CallbackQuery, db: Database, settings: Settings):
    user = await db.get_user(callback.from_user.id)
    if not user:
        await db.upsert_user(
            callback.from_user.id, settings.academic_year, default_reminder=settings.default_reminder_minutes
        )
        user = await db.get_user(callback.from_user.id)
    text = (
        "⚙️ <b>Уведомления</b>\n\n"
        f"Перед уроком: {'✅' if user['notifications'] else '❌'}\n"
        f"За сколько: {user['reminder_minutes']} мин\n"
        f"Изменения/замены: {'✅' if user['replacement_notifications'] else '❌'}\n"
        f"Утренняя сводка: {'✅' if user['morning_summary'] else '❌'}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔔 Перед уроком", callback_data="setting:notifications")],
            [
                InlineKeyboardButton(text="⚡ Изменения", callback_data="setting:replacement_notifications"),
                InlineKeyboardButton(text="🌅 Сводка", callback_data="setting:morning_summary"),
            ],
            [InlineKeyboardButton(text=f"⏱ {m} мин", callback_data=f"remind:{m}") for m in (5, 10, 15)],
            [InlineKeyboardButton(text=f"⏱ {m} мин", callback_data=f"remind:{m}") for m in (20, 30, 45)],
            [InlineKeyboardButton(text="← На главную", callback_data="home")],
        ]
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("setting:"))
async def toggle_setting(callback: CallbackQuery, db: Database, settings: Settings):
    field = callback.data.split(":", 1)[1]
    user = await db.get_user(callback.from_user.id)
    await db.update_user_setting(callback.from_user.id, field, 0 if user[field] else 1)
    await settings_screen(callback, db, settings)


@router.callback_query(F.data.startswith("remind:"))
async def reminder(callback: CallbackQuery, db: Database):
    minutes = int(callback.data.split(":", 1)[1])
    await db.update_user_setting(callback.from_user.id, "reminder_minutes", minutes)
    await callback.answer(f"Напоминание за {minutes} мин")
    user = await db.get_user(callback.from_user.id)
    text = f"✅ Напоминания будут приходить за <b>{user['reminder_minutes']} минут</b> до урока."
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_home())
