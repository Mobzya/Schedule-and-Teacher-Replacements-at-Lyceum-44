from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from .domain import EffectiveLesson
from .utils import RU_WEEKDAYS, esc

if TYPE_CHECKING:
    from aiogram.types import InlineKeyboardMarkup

HELP_TEXT = (
    "❔ <b>Как пользоваться ботом</b>\n\n"
    "🎓 Выберите свой класс; профиль появится, если он есть в расписании.\n"
    "📅 В расписании листайте дни стрелками. В «Неделе» нажмите нужную дату.\n"
    "⚡ Молния отмечает замену, зачёркнутый урок — отмену.\n"
    "⚙️ В настройках включаются напоминания и сообщения о заменах.\n\n"
    "👥 Друзья — личный список имён и классов с текущими и следующими уроками.\n\n"
    "/today — сегодня · /tomorrow — завтра\n"
    "/week — неделя · /changes — замены\n"
    "/friends — друзья · /settings — настройки\n"
    "/cancel — отменить ввод\n"
    "/start — главное меню"
)


def bells_text(bells, class_display: str) -> str:
    lines = [f"🔔 <b>Звонки</b> · {esc(class_display)}", ""]
    if bells:
        lines.extend(f"<b>{number}.</b> {start:%H:%M}–{end:%H:%M}" for number, (start, end) in bells.items())
    else:
        lines.append("Время звонков для этого класса пока не настроено.")
    return "\n".join(lines)


def main_keyboard(is_admin: bool = False, class_selected: bool = True) -> InlineKeyboardMarkup:
    from aiogram.types import InlineKeyboardMarkup

    return InlineKeyboardMarkup(**menu_markup(is_admin, class_selected))


def button(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def markup(*rows: list[dict]) -> dict:
    return {"inline_keyboard": list(rows)}


def menu_markup(is_admin: bool = False, class_selected: bool = True) -> dict:
    if not class_selected:
        rows = [
            [button("🎓 Выбрать класс", "class:choose")],
            [button("👥 Друзья", "f:list:0"), button("⚙️ Настройки", "settings")],
        ]
    else:
        rows = [
            [button("📅 Расписание", "today"), button("⚡ Список замен", "changes")],
            [button("👥 Друзья", "f:list:0"), button("⚙️ Настройки", "settings")],
        ]
    return {"inline_keyboard": rows}


def class_picker(classes, grade: str | None = None, selected: str | None = None) -> tuple[str, dict]:
    names = sorted({name for name, _ in classes}, key=lambda name: (class_grade(name), name))
    grades = sorted({class_grade(name) for name in names})
    if not names:
        return "🎓 <b>Классы ещё загружаются</b>\n\nОбновите список через несколько минут.", markup(
            [button("🔄 Обновить список", "class:choose")], [button("← Меню", "home")]
        )
    if grade is None:
        rows = [
            [button(f"{value} классы", f"grade:{value}") for value in grades[i : i + 3]]
            for i in range(0, len(grades), 3)
        ]
        title = "🎓 <b>Выберите параллель</b>"
    else:
        names = [name for name in names if str(class_grade(name)) == grade]
        rows = [
            [button(("✓ " if name == selected else "") + name.upper(), f"classn:{name}") for name in names[i : i + 3]]
            for i in range(0, len(names), 3)
        ]
        title = f"🎓 <b>{esc(grade)} классы</b> · выберите свой"
        if not names:
            title = "Список классов изменился. Вернитесь к выбору параллели."
        rows.append([button("← Все параллели", "class:choose")])
    rows.append([button("← Меню", "home")])
    return title, {"inline_keyboard": rows}


def class_grade(name: str) -> int:
    return int("".join(filter(str.isdigit, name)) or 0)


def day_markup(day: date, today: date) -> dict:
    offset = ((day - timedelta(days=day.weekday())) - (today - timedelta(days=today.weekday()))).days // 7
    return markup(
        [button("‹ День", f"day:{day - timedelta(days=1)}"), button("День ›", f"day:{day + timedelta(days=1)}")],
        [
            button("• Сегодня" if day == today else "Сегодня", "today"),
            button("• Завтра" if day == today + timedelta(days=1) else "Завтра", "tomorrow"),
        ],
        [button("📆 Вся неделя", f"week:{offset}"), button("← Меню", "home")],
    )


def week_markup(monday: date, offset: int) -> dict:
    days = [monday + timedelta(days=i) for i in range(7)]
    labels = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
    rows = [
        [button(f"{labels[i]} {days[i]:%d.%m}", f"day:{days[i]}") for i in range(start, min(start + 4, 7))]
        for start in (0, 4)
    ]
    nav = []
    if offset > -8:
        nav.append(button("‹ Неделя", f"week:{offset - 1}"))
    nav.append(button("Текущая", "week:0"))
    if offset < 8:
        nav.append(button("Неделя ›", f"week:{offset + 1}"))
    return markup(*rows, nav, [button("← Меню", "home")])


def settings_view(user, class_display: str, is_admin: bool = False) -> tuple[str, dict]:
    rows = [
        [button("🎓 Мой класс", "class:choose"), button("🔔 Уведомления", "settings:notifications")],
        [button("👥 Настроить друзей", "f:manage:0")],
        [button("🕘 Звонки", "bells"), button("❔ Помощь", "help")],
    ]
    if is_admin:
        rows.append([button("🛠 Администрирование", "admin")])
    rows.append([button("← Меню", "home")])
    return f"⚙️ <b>Настройки</b> · {esc(class_display)}", markup(*rows)


def notification_settings_view(user, class_display: str) -> tuple[str, dict]:
    def toggle(label, field):
        return button(f"{'✅' if user[field] else '○'} {label}", f"setting:{field}")

    text = (
        f"⚙️ <b>Настройки</b> · {esc(class_display)}\n\n"
        "Нажмите на уведомление, чтобы включить или выключить его.\n"
        "Интервал ниже — за сколько минут напоминать об уроке."
    )
    return text, markup(
        [toggle("Перед уроком", "notifications")],
        [toggle("Замены", "replacement_notifications"), toggle("Утренняя сводка", "morning_summary")],
        *[
            [
                button(f"{'✓ ' if minute == user['reminder_minutes'] else ''}{minute} мин", f"remind:{minute}")
                for minute in group
            ]
            for group in ((5, 10, 15), (20, 30, 45))
        ],
        [button("← Настройки", "settings")],
    )


def short(value, limit: int = 100) -> str:
    value = str(value or "")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def fit_lines(
    lines: list[str],
    limit: int = 3900,
    truncated_message: str = "\n… Откройте отдельный день, чтобы увидеть подробности.",
) -> str:
    """Keep complete HTML lines instead of cutting tags/entities at 4096 chars."""
    result = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > limit - 70:
            result.append(truncated_message)
            break
        result.append(line)
        size += len(line) + 1
    return "\n".join(result)


def render_week(monday: date, class_display: str, days: list[dict]) -> str:
    lines = [f"📆 <b>{monday:%d.%m}–{monday + timedelta(days=6):%d.%m}</b> · {esc(class_display)}"]
    for i, info in enumerate(days):
        day = monday + timedelta(days=i)
        lines.extend(["", f"<b>{RU_WEEKDAYS[i].capitalize()} · {day:%d.%m}</b>"])
        if info["state"] == "ready":
            lessons = info["lessons"]
            changed = sum(lesson.changed for lesson in lessons)
            subjects = " · ".join(short(lesson.subject, 24) for lesson in lessons if not lesson.cancelled)
            lines.append(esc(short(subjects, 190)) or "Все уроки отменены")
            if changed:
                lines.append(f"⚡ Изменений: {changed}")
        else:
            lines.append(esc(short(info["message"], 130)))
    lines.extend(["", "Нажмите на день ниже — откроются уроки, кабинеты и время."])
    return fit_lines(lines)


def back_home() -> InlineKeyboardMarkup:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← На главную", callback_data="home")]])


def render_waiting(message: str) -> str:
    return f"🏫 <b>Лицей №44</b>\n\n🟡 {esc(message)}"


def lesson_line(lesson: EffectiveLesson) -> str:
    if lesson.cancelled:
        return f"❌ <s>{lesson.lesson_number}. {esc(short(lesson.subject))}</s> · отменён"
    marker = "⚡ " if lesson.changed else ""
    time_text = f" · {lesson.start_time.strftime('%H:%M')}" if lesson.start_time else ""
    room = f" · каб. {esc(short(lesson.room, 30))}" if lesson.room else ""
    teacher = f" · {esc(short(lesson.teacher, 70))}" if lesson.changed and lesson.teacher else ""
    return f"{marker}<b>{lesson.lesson_number}. {esc(short(lesson.subject))}</b>{room}{teacher}{time_text}"


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
    return fit_lines(lines)
