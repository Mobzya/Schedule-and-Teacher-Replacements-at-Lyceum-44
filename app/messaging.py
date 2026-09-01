from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter


def split_text(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.extend(line[i : i + limit] for i in range(0, len(line), limit))
        elif len(current) + len(line) > limit:
            parts.append(current)
            current = line
        else:
            current += line
    if current:
        parts.append(current)
    return parts


async def send_chunks(bot: Bot, chat_id: int, text: str, parse_mode: str | None = "HTML") -> None:
    for chunk in split_text(text):
        await bot.send_message(chat_id, chunk, parse_mode=parse_mode)


async def broadcast(bot: Bot, user_ids: list[int], text: str) -> dict[str, int]:
    ok = blocked = failed = 0
    for uid in user_ids:
        try:
            await send_chunks(bot, uid, text)
            ok += 1
        except TelegramForbiddenError:
            blocked += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after))
            try:
                await send_chunks(bot, uid, text)
                ok += 1
            except Exception:
                failed += 1
        except (TelegramBadRequest, Exception):
            failed += 1
        await asyncio.sleep(0.035)
    return {"ok": ok, "blocked": blocked, "failed": failed}
