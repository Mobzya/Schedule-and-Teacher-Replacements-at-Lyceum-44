from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _json_dict(name: str) -> dict[str, str]:
    raw = os.getenv(name, "{}").strip() or "{}"
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{name} must be a JSON object")
    return {str(k): str(v) for k, v in data.items()}


def _admin_ids() -> frozenset[int]:
    raw = os.getenv("ADMIN_IDS", "")
    return frozenset(int(x.strip()) for x in raw.split(",") if x.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    timezone_name: str
    academic_year: str
    database_path: Path
    port: int
    source_sync_minutes: int
    notification_tick_seconds: int
    default_reminder_minutes: int
    bootstrap_schedule_sources: dict[str, str]
    bootstrap_replacement_sources: dict[str, str]
    bell_schedules: dict[str, list[list[str]]]
    class_bell_schedules: dict[str, str]

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)


def load_settings(require_token: bool = True) -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if require_token and not token:
        raise RuntimeError("BOT_TOKEN is not configured")
    db = Path(os.getenv("DATABASE_PATH", "./data/schedule_v3.db")).expanduser()
    return Settings(
        bot_token=token,
        admin_ids=_admin_ids(),
        timezone_name=os.getenv("TIMEZONE", "Europe/Moscow"),
        academic_year=os.getenv("ACADEMIC_YEAR", "2026-2027"),
        database_path=db,
        port=int(os.getenv("PORT", "10000")),
        source_sync_minutes=max(1, int(os.getenv("SOURCE_SYNC_MINUTES", "10"))),
        notification_tick_seconds=max(10, int(os.getenv("NOTIFICATION_TICK_SECONDS", "30"))),
        default_reminder_minutes=max(1, int(os.getenv("DEFAULT_REMINDER_MINUTES", "15"))),
        bootstrap_schedule_sources=_json_dict("BOOTSTRAP_SCHEDULE_SOURCES_JSON"),
        bootstrap_replacement_sources=_json_dict("BOOTSTRAP_REPLACEMENT_SOURCES_JSON"),
        bell_schedules=json.loads(
            os.getenv(
                "BELL_SCHEDULES_JSON",
                '{"main":[["08:15","09:00"],["09:15","10:00"],["10:15","11:00"],["11:15","12:00"],["12:15","13:00"],["13:15","14:00"],["14:15","15:00"],["15:15","16:00"]]}',
            )
        ),
        class_bell_schedules=_json_dict("CLASS_BELL_SCHEDULES_JSON"),
    )
