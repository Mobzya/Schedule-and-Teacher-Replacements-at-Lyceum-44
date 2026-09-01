from __future__ import annotations

from datetime import date, datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..config import Settings
from ..db import Database
from ..domain import PublicationState, SourceType
from ..messaging import broadcast
from ..services.sync import SyncService
from ..utils import esc

router = Router()


class AdminFlow(StatesGroup):
    source_date = State()
    source_url = State()
    waiting_message = State()
    broadcast_text = State()


def _admin(settings: Settings, user_id: int) -> bool:
    return user_id in settings.admin_ids


def _admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📡 Источники", callback_data="admin:sources"),
                InlineKeyboardButton(text="🔄 Синхронизация", callback_data="admin:sync"),
            ],
            [
                InlineKeyboardButton(text="🚦 Публикация", callback_data="admin:publication"),
                InlineKeyboardButton(text="📣 Рассылка", callback_data="admin:broadcast"),
            ],
            [InlineKeyboardButton(text="✏️ Сообщение ожидания", callback_data="admin:waiting_message")],
            [InlineKeyboardButton(text="← Ученический интерфейс", callback_data="home")],
        ]
    )


async def admin_text(db: Database) -> str:
    users = await db.all_users()
    sources = await db.list_sources()
    state = await db.publication_state()
    good = sum(1 for s in sources if s["last_status"] == "ok" and s["enabled"])
    bad = sum(1 for s in sources if s["last_status"] == "error" and s["enabled"])
    return (
        "🛠 <b>Администрирование</b>\n\n"
        f"🚦 Публикация: <b>{state.value}</b>\n"
        f"👥 Пользователей: {len(users)}\n"
        f"📡 Источников: {len(sources)} · ✅ {good} · ❌ {bad}\n\n"
        "Добавление Google Sheets, проверка и публикация выполняются прямо отсюда — менять `.env` после запуска не требуется."
    )


@router.message(Command("admin"))
async def admin_cmd(message: Message, settings: Settings, db: Database):
    if not _admin(settings, message.from_user.id):
        return
    await message.answer(await admin_text(db), parse_mode="HTML", reply_markup=_admin_kb())


@router.callback_query(F.data == "admin")
async def admin_home(callback: CallbackQuery, settings: Settings, db: Database):
    if not _admin(settings, callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(await admin_text(db), parse_mode="HTML", reply_markup=_admin_kb())
    await callback.answer()


@router.callback_query(F.data == "admin:sources")
async def sources(callback: CallbackQuery, settings: Settings, db: Database):
    if not _admin(settings, callback.from_user.id):
        return
    srcs = await db.list_sources()
    lines = ["📡 <b>Источники данных</b>", ""]
    rows = []
    for s in srcs:
        icon = (
            "✅"
            if s["last_status"] == "ok"
            else "❌"
            if s["last_status"] == "error"
            else "🟡"
            if s["last_status"] == "stale"
            else "⚪"
        )
        active = "🟢" if s["enabled"] else "⏸"
        lines.append(f"{active}{icon} #{s['id']} · {esc(s['name'])} · <code>{s['source_type']}</code>")
        rows.append(
            [InlineKeyboardButton(text=f"#{s['id']} {s['name'][:28]}", callback_data=f"admin:source:{s['id']}")]
        )
    if not srcs:
        lines.append("Источников пока нет.")
    rows += [
        [InlineKeyboardButton(text="➕ Основное расписание", callback_data="admin:add:weekly")],
        [InlineKeyboardButton(text="➕ Расписание на дату", callback_data="admin:add:date")],
        [InlineKeyboardButton(text="➕ Замены", callback_data="admin:add:replacements")],
        [InlineKeyboardButton(text="← Админ", callback_data="admin")],
    ]
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:source:"))
async def source_detail(callback: CallbackQuery, settings: Settings, db: Database):
    if not _admin(settings, callback.from_user.id):
        return
    sid = int(callback.data.rsplit(":", 1)[1])
    s = await db.get_source(sid)
    if not s:
        await callback.answer("Источник уже удалён", show_alert=True)
        return
    text = (
        f"📡 <b>{esc(s['name'])}</b>\n\n"
        f"ID: {s['id']}\nТип: <code>{s['source_type']}</code>\n"
        f"Статус: {esc(s['last_status'])}\n"
        f"Активен: {'да' if s['enabled'] else 'нет'}\n"
        f"Приоритет: {s['priority']}\n"
        f"Период: {s['valid_from'] or '—'} → {s['valid_until'] or '—'}\n"
        f"Последний успех: {s['last_success_at'] or '—'}\n"
        f"Ошибка: {esc(s['last_error'] or '—')}\n\n"
        f"<code>{esc(s['url'])}</code>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Проверить сейчас", callback_data=f"admin:source_sync:{sid}")],
            [
                InlineKeyboardButton(
                    text="⏸ Отключить" if s["enabled"] else "▶️ Включить", callback_data=f"admin:source_toggle:{sid}"
                )
            ],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"admin:source_delete_confirm:{sid}")],
            [InlineKeyboardButton(text="← Источники", callback_data="admin:sources")],
        ]
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:source_sync:"))
async def source_sync(callback: CallbackQuery, settings: Settings, sync: SyncService):
    if not _admin(settings, callback.from_user.id):
        return
    sid = int(callback.data.rsplit(":", 1)[1])
    await callback.answer("Проверяю…")
    try:
        result = await sync.sync_source(sid)
        await callback.message.edit_text(
            f"✅ Источник проверен и snapshot обновлён.\nЗаписей: <b>{result['count']}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="← К источнику", callback_data=f"admin:source:{sid}")]]
            ),
        )
    except Exception as exc:
        await callback.message.edit_text(
            f"❌ Источник не принят. Рабочие данные не изменены.\n\n<code>{esc(exc)}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="← К источнику", callback_data=f"admin:source:{sid}")]]
            ),
        )


@router.callback_query(F.data.startswith("admin:source_toggle:"))
async def source_toggle(callback: CallbackQuery, settings: Settings, db: Database):
    if not _admin(settings, callback.from_user.id):
        return
    sid = int(callback.data.rsplit(":", 1)[1])
    s = await db.get_source(sid)
    await db.set_source_enabled(sid, not bool(s["enabled"]), callback.from_user.id)
    updated = await db.get_source(sid)
    await callback.message.edit_text(
        f"{'▶️ Источник включён' if updated['enabled'] else '⏸ Источник отключён'}.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="← К источнику", callback_data=f"admin:source:{sid}")]]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:source_delete_confirm:"))
async def source_delete_confirm(callback: CallbackQuery, settings: Settings):
    if not _admin(settings, callback.from_user.id):
        return
    sid = int(callback.data.rsplit(":", 1)[1])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"admin:source_delete:{sid}"),
                InlineKeyboardButton(text="Отмена", callback_data=f"admin:source:{sid}"),
            ]
        ]
    )
    await callback.message.edit_text(
        "Удалить источник и его snapshot? Это не затронет другие источники.", reply_markup=kb
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:source_delete:"))
async def source_delete(callback: CallbackQuery, settings: Settings, db: Database):
    if not _admin(settings, callback.from_user.id):
        return
    sid = int(callback.data.rsplit(":", 1)[1])
    await db.delete_source(sid, callback.from_user.id)
    await callback.message.edit_text(
        "✅ Источник удалён.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="← Источники", callback_data="admin:sources")]]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:add:"))
async def add_source_start(callback: CallbackQuery, state: FSMContext, settings: Settings):
    if not _admin(settings, callback.from_user.id):
        return
    kind = callback.data.rsplit(":", 1)[1]
    source_type = {
        "weekly": SourceType.WEEKLY.value,
        "date": SourceType.DATE.value,
        "replacements": SourceType.REPLACEMENTS.value,
    }[kind]
    await state.clear()
    await state.update_data(source_type=source_type)
    if source_type == SourceType.DATE.value:
        await state.set_state(AdminFlow.source_date)
        await callback.message.edit_text(
            "📅 Введите дату, для которой действует временное расписание, в формате <code>02.09.2026</code>.",
            parse_mode="HTML",
        )
    else:
        await state.set_state(AdminFlow.source_url)
        await callback.message.edit_text(
            "🔗 Пришлите обычную ссылку Google Sheets. Если нужна конкретная вкладка, откройте её перед копированием ссылки — бот сам извлечёт <code>spreadsheetId</code> и <code>gid</code>.",
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(AdminFlow.source_date)
async def source_date(message: Message, state: FSMContext, settings: Settings):
    if not _admin(settings, message.from_user.id):
        return
    try:
        d = datetime.strptime(message.text.strip(), "%d.%m.%Y").date()
    except Exception:
        await message.answer("Не понял дату. Пример: <code>02.09.2026</code>", parse_mode="HTML")
        return
    await state.update_data(valid_date=d.isoformat())
    await state.set_state(AdminFlow.source_url)
    await message.answer("Теперь пришлите ссылку Google Sheets на однодневное расписание.")


@router.message(AdminFlow.source_url)
async def source_url(message: Message, state: FSMContext, settings: Settings, sync: SyncService):
    if not _admin(settings, message.from_user.id):
        return
    data = await state.get_data()
    url = (message.text or "").strip()
    stype = SourceType(data["source_type"])
    forced = date.fromisoformat(data["valid_date"]) if data.get("valid_date") else None
    await message.answer("🔎 Проверяю таблицу. Production-данные пока не меняются…")
    try:
        result = await sync.preview(stype, url, forced, settings.academic_year)
    except Exception as exc:
        await message.answer(
            f"❌ Таблица не прошла проверку:\n<code>{esc(exc)}</code>\n\nПопробуйте другую ссылку или /admin.",
            parse_mode="HTML",
        )
        await state.clear()
        return
    await state.update_data(url=url, preview_count=result["count"], declared_year=result.get("declared_year"))
    date_line = f"\nДата: <b>{result['date'].strftime('%d.%m.%Y')}</b>" if result.get("date") else ""
    classes = ", ".join(x.upper() for x in result.get("classes", [])) or "—"
    text = f"✅ <b>Источник распознан</b>\n\nТип: <code>{stype.value}</code>\nЗаписей: <b>{result['count']}</b>\nКлассы: <b>{esc(classes)}</b>\nУчебный год в листе: {esc(result.get('declared_year') or 'не указан')}{date_line}\n\nСохранить этот источник?"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Сохранить", callback_data="admin:add_confirm"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="admin:sources"),
            ]
        ]
    )
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "admin:add_confirm")
async def add_source_confirm(
    callback: CallbackQuery, state: FSMContext, settings: Settings, db: Database, sync: SyncService
):
    if not _admin(settings, callback.from_user.id):
        return
    data = await state.get_data()
    if not data.get("url"):
        await callback.answer("Сессия добавления устарела", show_alert=True)
        return
    stype = SourceType(data["source_type"])
    valid = date.fromisoformat(data["valid_date"]) if data.get("valid_date") else None
    stamp = datetime.now().strftime("%d.%m %H:%M:%S")
    label = (
        "Основное"
        if stype == SourceType.WEEKLY
        else "Замены"
        if stype == SourceType.REPLACEMENTS
        else f"День {valid.strftime('%d.%m')}"
    )
    name = f"{label} · {stamp}"
    sid = await db.add_source(
        name,
        stype,
        data["url"],
        500 if stype == SourceType.DATE else 100,
        valid,
        valid if stype == SourceType.DATE else None,
        settings.academic_year,
        callback.from_user.id,
    )
    try:
        result = await sync.sync_source(sid)
    except Exception as exc:
        await db.delete_source(sid, callback.from_user.id)
        await callback.message.edit_text(
            f"❌ Между предпросмотром и сохранением источник перестал проходить проверку. Ничего не сохранено.\n<code>{esc(exc)}</code>",
            parse_mode="HTML",
            reply_markup=_admin_kb(),
        )
        await state.clear()
        return
    await state.clear()
    await callback.message.edit_text(
        f"✅ Источник сохранён. Записей: {result['count']}.\n\nОн уже участвует в черновом snapshot, но ученики увидят расписание только после <b>публикации</b>.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🚦 Перейти к публикации", callback_data="admin:publication")],
                [InlineKeyboardButton(text="📡 Источники", callback_data="admin:sources")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:sync")
async def sync_all(callback: CallbackQuery, settings: Settings, sync: SyncService):
    if not _admin(settings, callback.from_user.id):
        return
    await callback.answer("Запускаю синхронизацию…")
    result = await sync.sync_all()
    await callback.message.edit_text(
        f"🔄 <b>Синхронизация завершена</b>\n\n✅ Успешно: {result['ok']}\n❌ Ошибок: {result['failed']}\n⏭ Пропущено: {result['skipped']}",
        parse_mode="HTML",
        reply_markup=_admin_kb(),
    )


@router.callback_query(F.data == "admin:publication")
async def publication(callback: CallbackQuery, settings: Settings, db: Database):
    if not _admin(settings, callback.from_user.id):
        return
    state = await db.publication_state()
    sources = await db.list_sources(True)
    good_sched = [
        s
        for s in sources
        if s["last_status"] == "ok" and s["source_type"] in (SourceType.WEEKLY.value, SourceType.DATE.value)
    ]
    text = f"🚦 <b>Публикация</b>\n\nТекущее состояние: <b>{state.value}</b>\nПроверенных источников расписания: <b>{len(good_sched)}</b>\n\nВ WAITING ученики видят сообщение о подготовке расписания. В READY отображаются сохранённые snapshot-ы."
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Опубликовать + уведомить", callback_data="admin:publish")],
            [InlineKeyboardButton(text="🟡 Вернуть в ожидание", callback_data="admin:set_waiting")],
            [
                InlineKeyboardButton(
                    text="📣 Напомнить всем: расписание ожидается", callback_data="admin:notify_waiting"
                )
            ],
            [InlineKeyboardButton(text="← Админ", callback_data="admin")],
        ]
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "admin:publish")
async def publish(callback: CallbackQuery, bot: Bot, settings: Settings, db: Database):
    if not _admin(settings, callback.from_user.id):
        return
    sources = await db.list_sources(True)
    if not any(
        s["last_status"] == "ok" and s["source_type"] in (SourceType.WEEKLY.value, SourceType.DATE.value)
        for s in sources
    ):
        await callback.answer("Нет ни одного успешно проверенного источника расписания", show_alert=True)
        return
    await db.set_publication_state(PublicationState.READY, callback.from_user.id)
    users = await db.all_users()
    text = "✅ <b>Расписание опубликовано</b>\n\nБот получил расписание на новый учебный год. Откройте бота и выберите актуальный класс — после этого станут доступны «Сегодня», неделя и уведомления."
    result = await broadcast(bot, [int(u["user_id"]) for u in users], text)
    await callback.message.edit_text(
        f"✅ Расписание опубликовано.\n\nУведомления: {result['ok']} доставлено, {result['blocked']} заблокировали бота, {result['failed']} ошибок.",
        reply_markup=_admin_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:set_waiting")
async def set_waiting(callback: CallbackQuery, settings: Settings, db: Database):
    if not _admin(settings, callback.from_user.id):
        return
    await db.set_publication_state(PublicationState.WAITING, callback.from_user.id)
    await callback.message.edit_text(
        "🟡 Режим ожидания включён. Ученики не увидят черновые snapshot-ы.", reply_markup=_admin_kb()
    )
    await callback.answer()


@router.callback_query(F.data == "admin:notify_waiting")
async def notify_waiting(callback: CallbackQuery, bot: Bot, settings: Settings, db: Database):
    if not _admin(settings, callback.from_user.id):
        return
    users = await db.all_users()
    msg = await db.waiting_message()
    text = f"🟡 <b>Расписание ещё формируется</b>\n\n{esc(msg)}\n\nКак только оно будет проверено и опубликовано, бот пришлёт отдельное уведомление."
    result = await broadcast(bot, [int(u["user_id"]) for u in users], text)
    await callback.message.edit_text(
        f"📣 Готово. Доставлено: {result['ok']}; заблокировали бота: {result['blocked']}; ошибок: {result['failed']}.",
        reply_markup=_admin_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:waiting_message")
async def waiting_message_start(callback: CallbackQuery, state: FSMContext, settings: Settings, db: Database):
    if not _admin(settings, callback.from_user.id):
        return
    current = await db.waiting_message()
    await state.set_state(AdminFlow.waiting_message)
    await callback.message.edit_text(
        f"✏️ Текущее сообщение:\n\n{esc(current)}\n\nПришлите новый текст одним сообщением.", parse_mode="HTML"
    )
    await callback.answer()


@router.message(AdminFlow.waiting_message)
async def waiting_message_save(message: Message, state: FSMContext, settings: Settings, db: Database):
    if not _admin(settings, message.from_user.id):
        return
    text = (message.text or "").strip()
    if not 10 <= len(text) <= 1000:
        await message.answer("Текст должен быть длиной 10–1000 символов.")
        return
    await db.set_waiting_message(text, message.from_user.id)
    await state.clear()
    await message.answer("✅ Сообщение ожидания обновлено.", reply_markup=_admin_kb())


@router.callback_query(F.data == "admin:broadcast")
async def broadcast_start(callback: CallbackQuery, state: FSMContext, settings: Settings):
    if not _admin(settings, callback.from_user.id):
        return
    await state.set_state(AdminFlow.broadcast_text)
    await callback.message.edit_text("📣 Пришлите текст рассылки. HTML разрешён; перед отправкой будет предпросмотр.")
    await callback.answer()


@router.message(AdminFlow.broadcast_text)
async def broadcast_preview(message: Message, state: FSMContext, settings: Settings):
    if not _admin(settings, message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text:
        return
    await state.update_data(broadcast=text)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📣 Отправить всем", callback_data="admin:broadcast_confirm"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="admin"),
            ]
        ]
    )
    try:
        await message.answer("<b>Предпросмотр:</b>\n\n" + text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await message.answer("В тексте некорректный HTML. Исправьте сообщение и пришлите снова.")


@router.callback_query(F.data == "admin:broadcast_confirm")
async def broadcast_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot, settings: Settings, db: Database):
    if not _admin(settings, callback.from_user.id):
        return
    data = await state.get_data()
    text = data.get("broadcast")
    if not text:
        await callback.answer("Черновик устарел", show_alert=True)
        return
    result = await broadcast(bot, [int(u["user_id"]) for u in await db.all_users()], text)
    await state.clear()
    await callback.message.edit_text(
        f"📣 Доставлено: {result['ok']}; заблокировали: {result['blocked']}; ошибок: {result['failed']}.",
        reply_markup=_admin_kb(),
    )
    await callback.answer()


@router.message(Command("dayoff"))
async def dayoff_cmd(message: Message, settings: Settings, db: Database):
    if not _admin(settings, message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Использование: <code>/dayoff YYYY-MM-DD [причина]</code>", parse_mode="HTML")
        return
    try:
        day = date.fromisoformat(parts[1])
    except ValueError:
        await message.answer("Некорректная дата.")
        return
    note = parts[2] if len(parts) > 2 else "Неучебный день"
    await db.set_calendar_day(day, False, None, note, message.from_user.id)
    await message.answer(f"✅ {day.strftime('%d.%m.%Y')} отмечен как неучебный день.")


@router.message(Command("schoolday"))
async def schoolday_cmd(message: Message, settings: Settings, db: Database):
    if not _admin(settings, message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 2:
        await message.answer(
            "Использование: <code>/schoolday YYYY-MM-DD [0-6] [причина]</code>, где 0=понедельник.", parse_mode="HTML"
        )
        return
    try:
        day = date.fromisoformat(parts[1])
    except ValueError:
        await message.answer("Некорректная дата.")
        return
    weekday = day.weekday()
    if len(parts) > 2 and parts[2].isdigit():
        weekday = max(0, min(6, int(parts[2])))
    note = parts[3] if len(parts) > 3 else "Учебный день"
    await db.set_calendar_day(day, True, weekday, note, message.from_user.id)
    await message.answer(f"✅ {day.strftime('%d.%m.%Y')} отмечен как учебный день по расписанию weekday={weekday}.")


@router.message(Command("dayclear"))
async def dayclear_cmd(message: Message, settings: Settings, db: Database):
    if not _admin(settings, message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: <code>/dayclear YYYY-MM-DD</code>", parse_mode="HTML")
        return
    try:
        day = date.fromisoformat(parts[1])
    except ValueError:
        await message.answer("Некорректная дата.")
        return
    await db.clear_calendar_day(day)
    await message.answer("✅ Исключение календаря удалено.")
