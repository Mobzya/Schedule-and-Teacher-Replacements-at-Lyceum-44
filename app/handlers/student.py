from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..config import Settings
from ..db import Database
from ..domain import PublicationState
from ..services.schedule import ScheduleService
from ..ui import (
    HELP_TEXT,
    back_home,
    bells_text,
    class_picker,
    day_markup,
    fit_lines,
    lesson_line,
    main_keyboard,
    notification_settings_view,
    render_day,
    render_waiting,
    render_week,
    settings_view,
    week_markup,
)
from ..utils import RU_WEEKDAYS, esc, now_local

router = Router()


@router.callback_query.middleware()
async def leave_input_on_navigation(handler, event, data):
    # Inner middleware runs only after a student callback has matched; friend
    # callbacks live in their own router and keep their draft until completion.
    if state := data.get("state"):
        await state.clear()
    return await handler(event, data)


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
        title, keyboard = class_picker(await db.classes())
        keyboard["inline_keyboard"].append(
            [{"text": "👥 Друзья", "callback_data": "f:list:0"}, {"text": "⚙️ Настройки", "callback_data": "settings"}]
        )
        return f"👋 {esc(first_name)}!\n\n{title}", InlineKeyboardMarkup(**keyboard)
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
                        "Откройте «Расписание», чтобы увидеть все уроки.",
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
async def home(
    callback: CallbackQuery, db: Database, settings: Settings, schedules: ScheduleService, state: FSMContext
):
    await state.clear()
    text, kb = await render_home(
        callback.from_user.id, callback.from_user.first_name or "Ученик", db, settings, schedules
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "today")
@router.callback_query(F.data == "tomorrow")
@router.callback_query(F.data.startswith("day:"))
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
        current_date = now_local(settings.timezone).date()
        try:
            day = (
                date.fromisoformat(callback.data.split(":", 1)[1])
                if callback.data.startswith("day:")
                else current_date + timedelta(days=callback.data == "tomorrow")
            )
        except ValueError:
            await callback.answer("Кнопка устарела. Откройте /start.", show_alert=True)
            return
        if abs((day - current_date).days) > 56:
            await callback.answer("Доступны даты в пределах восьми недель", show_alert=True)
            return
        info = await schedules.day_schedule(user["class_name"], user["profile"], day)
        await callback.message.edit_text(
            render_day(day, _class_display(user), info),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(**day_markup(day, current_date)),
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
    days = [
        await schedules.day_schedule(user["class_name"], user["profile"], monday + timedelta(days=i)) for i in range(7)
    ]
    await callback.message.edit_text(
        render_week(monday, _class_display(user), days),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(**week_markup(monday, offset)),
    )
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
    await callback.message.edit_text(fit_lines(parts), parse_mode="HTML", reply_markup=back_home())
    await callback.answer()


@router.callback_query(F.data == "class:choose")
@router.callback_query(F.data.startswith("grade:"))
async def choose_class(callback: CallbackQuery, db: Database, settings: Settings):
    classes = await db.classes()
    user = await db.get_user(callback.from_user.id)
    grade = callback.data.split(":", 1)[1] if callback.data.startswith("grade:") else None
    text, keyboard = class_picker(classes, grade, user["class_name"] if user else None)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(**keyboard))
    await callback.answer()


@router.callback_query(F.data.startswith("classn:"))
async def choose_profile(callback: CallbackQuery, db: Database, settings: Settings, schedules: ScheduleService):
    class_name = callback.data.split(":", 1)[1]
    choices = [(c, p) for c, p in await db.classes() if c == class_name]
    if not choices:
        await callback.answer("Класс больше недоступен. Обновите список.", show_alert=True)
        return
    profiles = sorted({p for _, p in choices if p})
    if not profiles:
        await db.set_user_class(
            callback.from_user.id, class_name, None, settings.academic_year, settings.default_reminder_minutes
        )
        await callback.answer("Класс сохранён")
        text, keyboard = await render_home(
            callback.from_user.id, callback.from_user.first_name or "Ученик", db, settings, schedules
        )
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
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
async def set_profile(callback: CallbackQuery, db: Database, settings: Settings, schedules: ScheduleService):
    _, class_name, idx_s = callback.data.split(":", 2)
    profiles = sorted({p for c, p in await db.classes() if c == class_name and p})
    idx = int(idx_s)
    if idx < 0 or idx >= len(profiles):
        await callback.answer("Список изменился, выберите снова", show_alert=True)
        return
    await db.set_user_class(
        callback.from_user.id, class_name, profiles[idx], settings.academic_year, settings.default_reminder_minutes
    )
    text, keyboard = await render_home(
        callback.from_user.id, callback.from_user.first_name or "Ученик", db, settings, schedules
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer("Класс и профиль сохранены")


@router.callback_query(F.data == "settings")
@router.callback_query(F.data == "settings:notifications")
async def settings_screen(callback: CallbackQuery, db: Database, settings: Settings, state: FSMContext = None):
    if state:
        await state.clear()
    user = await db.get_user(callback.from_user.id)
    if not user:
        await db.upsert_user(
            callback.from_user.id, settings.academic_year, default_reminder=settings.default_reminder_minutes
        )
        user = await db.get_user(callback.from_user.id)
    text, keyboard = (
        settings_view(user, _class_display(user), _is_admin(settings, callback.from_user.id))
        if callback.data == "settings"
        else notification_settings_view(user, _class_display(user))
    )
    kb = InlineKeyboardMarkup(**keyboard)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("setting:"))
async def toggle_setting(callback: CallbackQuery, db: Database, settings: Settings):
    field = callback.data.split(":", 1)[1]
    if field not in {"notifications", "replacement_notifications", "morning_summary"}:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    user = await db.get_user(callback.from_user.id)
    if not user:
        await db.upsert_user(
            callback.from_user.id, settings.academic_year, default_reminder=settings.default_reminder_minutes
        )
        user = await db.get_user(callback.from_user.id)
    await db.update_user_setting(callback.from_user.id, field, 0 if user[field] else 1)
    await settings_screen(callback, db, settings)


@router.callback_query(F.data.startswith("remind:"))
async def reminder(callback: CallbackQuery, db: Database, settings: Settings):
    minutes = int(callback.data.split(":", 1)[1])
    if minutes not in (5, 10, 15, 20, 30, 45):
        await callback.answer("Недопустимый интервал", show_alert=True)
        return
    await db.update_user_setting(callback.from_user.id, "reminder_minutes", minutes)
    await settings_screen(callback, db, settings)


@router.callback_query(F.data == "help")
async def help_screen(callback: CallbackQuery):
    await callback.message.edit_text(HELP_TEXT, parse_mode="HTML", reply_markup=back_home())
    await callback.answer()


@router.callback_query(F.data == "bells")
async def bells(callback: CallbackQuery, db: Database, settings: Settings, schedules: ScheduleService):
    user = await db.get_user(callback.from_user.id)
    if not user or not user["class_name"] or user["academic_year"] != settings.academic_year:
        await choose_class(callback, db, settings)
        return
    text = bells_text(schedules.bell_times_for_class(user["class_name"]), _class_display(user))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_home())
    await callback.answer()


@router.message(Command("today", "tomorrow", "week", "changes", "settings", "help", "bells"))
async def command_screen(
    message: Message, db: Database, settings: Settings, schedules: ScheduleService, state: FSMContext
):
    await state.clear()
    command = message.text.split(maxsplit=1)[0].split("@", 1)[0][1:]

    async def answered(*args, **kwargs):
        return None

    # Reuse the same views for typed commands; a command sends a fresh panel,
    # while inline buttons edit the existing panel.
    event = SimpleNamespace(
        from_user=message.from_user,
        message=SimpleNamespace(edit_text=message.answer),
        data="week:0" if command == "week" else command,
        answer=answered,
    )
    if command in {"today", "tomorrow"}:
        await today(event, db, settings, schedules)
    elif command == "week":
        await week(event, db, settings, schedules)
    elif command == "changes":
        await changes(event, db, settings, schedules)
    elif command == "settings":
        await settings_screen(event, db, settings)
    elif command == "bells":
        await bells(event, db, settings, schedules)
    else:
        await help_screen(event)
