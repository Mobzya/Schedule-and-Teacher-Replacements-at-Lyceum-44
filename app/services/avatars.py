"""Small recurring calendar and durable, retryable bot-avatar rotation."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

AVATAR_CRON = "3-59/10 * * * *"
ASSET_REVISION = "strict-v1"


@dataclass(frozen=True)
class AvatarTheme:
    key: str
    title: str
    month: int = 0
    day: int = 0

    @property
    def filename(self) -> str:
        return f"{self.key}-{ASSET_REVISION}.jpg"

    @property
    def fingerprint(self) -> str:
        return f"{ASSET_REVISION}:{self.key}"


SEASONS = {
    "winter": AvatarTheme("winter", "Зима"),
    "spring": AvatarTheme("spring", "Весна"),
    "summer": AvatarTheme("summer", "Лето"),
    "autumn": AvatarTheme("autumn", "Осень"),
}
# Fixed illustrative holiday dates, not the school's academic calendar.
# Every holiday lasts exactly seven days: date - 3 through date + 3 inclusive.
HOLIDAYS = (
    AvatarTheme("new_year", "Новый год", 1, 1),
    AvatarTheme("student_day", "День студента", 1, 25),
    AvatarTheme("science_day", "День науки", 2, 8),
    AvatarTheme("womens_day", "8 Марта", 3, 8),
    AvatarTheme("cosmonautics_day", "День космонавтики", 4, 12),
    AvatarTheme("last_bell", "Последний звонок — тема к 25 мая", 5, 25),
    AvatarTheme("children_day", "День защиты детей", 6, 1),
    AvatarTheme("knowledge_day", "День знаний", 9, 1),
    AvatarTheme("teacher_day", "День учителя", 10, 5),
    AvatarTheme("halloween", "Хэллоуин", 10, 31),
)
ALL_THEMES = (*SEASONS.values(), *HOLIDAYS)


def theme_for_day(day: date) -> AvatarTheme:
    for theme in HOLIDAYS:
        # Include the adjacent year for Dec 29–Jan 4 without special cases.
        for year in (day.year - 1, day.year, day.year + 1):
            anchor = date(year, theme.month, theme.day)
            if abs((day - anchor).days) <= 3:
                return theme
    season = (
        "winter"
        if day.month in (12, 1, 2)
        else ("spring" if day.month in (3, 4, 5) else "summer" if day.month in (6, 7, 8) else "autumn")
    )
    return SEASONS[season]


class AvatarService:
    def __init__(self, db, timezone, upload: Callable[[AvatarTheme], Awaitable[None]]):
        self.db, self.timezone, self.upload = db, timezone, upload

    async def refresh(self, now: datetime, force: bool = False) -> bool:
        if now.tzinfo is None:
            raise ValueError("Avatar clock must include a timezone")
        now = now.astimezone(self.timezone)
        if await self.db.get_state("avatar_enabled", "1") != "1":
            return False
        target = theme_for_day(now.date())
        if not force and await self.db.get_state("avatar_current") == target.fingerprint:
            return False
        retry_at = await self.db.get_state("avatar_retry_at")
        if retry_at and not force:
            try:
                if now < datetime.fromisoformat(retry_at):
                    return False
            except (ValueError, TypeError):
                pass
        try:
            await self.upload(target)
        except Exception as exc:
            # Keep the last successful avatar; transient failures retry next cron.
            # Do not persist raw exceptions, which may contain token-bearing URLs.
            seconds = max(600, min(86400, int(getattr(exc, "retry_after", None) or 600)))
            await self.db.set_state("avatar_retry_at", (now + timedelta(seconds=seconds)).isoformat())
            error = {
                "type": type(exc).__name__,
                "theme": target.key,
                "at": now.isoformat(),
                "code": getattr(exc, "error_code", None),
            }
            if error["code"] == 400 or type(exc).__name__ == "JsException":
                # Telegram's validation reason is useful for diagnosing upload
                # errors. Remove URLs/token-shaped text before persisting it.
                detail = re.sub(r"https?://\S+", "<url>", str(exc))
                error["detail"] = re.sub(r"(?:bot)?\d+:[A-Za-z0-9_-]{20,}", "<token>", detail)[:250]
            await self.db.set_state("avatar_last_error", json.dumps(error))
            raise RuntimeError(f"Avatar upload failed: {type(exc).__name__}") from None
        # Only mark success after Telegram has confirmed the upload.
        await self.db.set_state("avatar_current", target.fingerprint)
        await self.db.set_state("avatar_updated_at", now.isoformat())
        await self.db.set_state("avatar_retry_at", "")
        await self.db.set_state("avatar_last_error", "")
        return True
