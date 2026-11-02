from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot


class TelegramAPIError(Exception):
    def __init__(self, message: str, error_code: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after


def _is_forbidden(exc: Exception) -> bool:
    return getattr(exc, "error_code", None) == 403 or "bot was blocked" in str(exc).lower()


def _retry_after(exc: Exception) -> int | None:
    return getattr(exc, "retry_after", None)


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
        except Exception as exc:
            if _is_forbidden(exc):
                blocked += 1
                await asyncio.sleep(0.035)
                continue
            retry_after = _retry_after(exc)
            if retry_after is not None:
                await asyncio.sleep(float(retry_after))
                try:
                    await send_chunks(bot, uid, text)
                    ok += 1
                except Exception:
                    failed += 1
            else:
                failed += 1
        await asyncio.sleep(0.035)
    return {"ok": ok, "blocked": blocked, "failed": failed}
