from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ..services.friends import FriendsService
from ..utils import esc, now_local

router = Router()


async def show(message, state, view, panel_id=None):
    text, keyboard, next_state, draft, _ = view
    result = None
    if panel_id:
        try:
            result = await message.bot.edit_message_text(
                text,
                chat_id=message.chat.id,
                message_id=panel_id,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(**keyboard),
            )
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                result = message
            elif (
                "message to edit not found" not in str(exc).lower()
                and "message can't be edited" not in str(exc).lower()
            ):
                raise
    if result is None:
        result = await message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(**keyboard))
        panel_id = result.message_id
    await state.clear()
    if next_state:
        await state.set_state(next_state)
        await state.set_data({**draft, "panel_message_id": panel_id or result.message_id})


@router.message(Command("friends"))
async def friends_command(message: Message, state: FSMContext, db, settings, schedules):
    if message.chat.id != message.from_user.id:
        await message.answer("Откройте список друзей в личном чате с ботом.")
        return
    await db.upsert_user(
        message.from_user.id, settings.academic_year, default_reminder=settings.default_reminder_minutes
    )
    view = await FriendsService(db, settings, schedules).page(message.from_user.id, 0, now_local(settings.timezone))
    await show(message, state, view)


@router.callback_query(F.data.startswith("f:"))
async def friends_callback(callback: CallbackQuery, state: FSMContext, db, settings, schedules):
    if callback.message.chat.id != callback.from_user.id:
        await callback.answer("Откройте друзей в личном чате с ботом.", show_alert=True)
        return
    await db.upsert_user(
        callback.from_user.id, settings.academic_year, default_reminder=settings.default_reminder_minutes
    )
    service = FriendsService(db, settings, schedules)
    try:
        view = await service.callback(
            callback.from_user.id,
            callback.data,
            await state.get_state(),
            await state.get_data(),
            now_local(settings.timezone),
        )
    except (ValueError, IndexError) as exc:
        await callback.answer(str(exc)[:190], show_alert=True)
        return
    await show(callback.message, state, view, callback.message.message_id)
    await callback.answer(view[4])


@router.message(StateFilter("friend:name"), F.text, ~F.text.startswith("/"), F.chat.type == "private")
async def friend_input(message: Message, state: FSMContext, db, settings, schedules):
    draft = await state.get_data()
    try:
        view = await FriendsService(db, settings, schedules).message(
            message.from_user.id, message.text.strip(), draft, now_local(settings.timezone)
        )
    except ValueError as exc:
        from ..ui import button, markup

        view = (esc(exc), markup([button("Отмена", "f:manage:0")]), "friend:name", draft, "")
    await show(message, state, view, draft.get("panel_message_id"))
