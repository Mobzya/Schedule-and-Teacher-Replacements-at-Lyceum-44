from __future__ import annotations

from datetime import date

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .domain import EffectiveLesson
from .utils import RU_WEEKDAYS, esc


def main_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="📅 Сегодня", callback_data="today"),
            InlineKeyboardButton(text="📆 Неделя", callback_data="week:0"),
        ],
        [
            InlineKeyboardButton(text="⚡ Изменения", callback_data="changes"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings"),
        ],
        [
            InlineKeyboardButton(text="🎓 Мой класс", callback_data="class:choose"),
            InlineKeyboardButton(text="🔄 Обновить", callback_data="home"),
        ],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="🛠 Админ", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← На главную", callback_data="home")]])


def render_waiting(message: str) -> str:
    return (
        "🏫 <b>Лицей №44 · расписание</b>\n\n"
        "🟡 <b>Расписание 2026/27 ещё не опубликовано</b>\n\n"
        f"{esc(message)}\n\n"
        "Бот уже работает. Когда администрация загрузит и проверит расписание, здесь автоматически появятся уроки, а вы получите уведомление."
    )


def lesson_line(lesson: EffectiveLesson) -> str:
    if lesson.cancelled:
        return f"❌ <s>{lesson.lesson_number}. {esc(lesson.subject)}</s> · отменён"
    marker = "⚡ " if lesson.changed else ""
    time_text = f" · {lesson.start_time.strftime('%H:%M')}" if lesson.start_time else ""
    room = f" · каб. {esc(lesson.room)}" if lesson.room else ""
    return f"{marker}<b>{lesson.lesson_number}. {esc(lesson.subject)}</b>{room}{time_text}"


def render_day(day: date, class_display: str, info: dict) -> str:
    title = f"📅 <b>{RU_WEEKDAYS[day.weekday()].capitalize()}, {day.strftime('%d.%m')}</b> · {esc(class_display)}"
    if info["state"] == "waiting":
        return render_waiting(info["message"])
    if info["state"] in {"day_off", "paused", "no_class_data"}:
        return f"{title}\n\nℹ️ {esc(info['message'])}"
    lines = [title, ""] + [lesson_line(x) for x in info["lessons"]]
    if not info.get("bells_known", True):
        lines += [
            "",
            "⚠️ Время звонков для этого класса ещё не настроено; напоминания по времени временно не отправляются.",
        ]
    return "\n".join(lines)
