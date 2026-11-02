from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
except ImportError:  # Cloudflare injects settings as Worker bindings.
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()

DEFAULT_ACADEMIC_YEAR = "2026-2027"
# Python Workers snapshot module initialization at deployment time. Loading the
# IANA file here keeps its initial parsing outside the Free request CPU budget.
_MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")


def _json_dict(name: str, source=os.environ) -> dict[str, str]:
    raw = source.get(name, "{}").strip() or "{}"
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{name} must be a JSON object")
    return {str(k): str(v) for k, v in data.items()}


def _admin_ids(source=os.environ) -> frozenset[int]:
    raw = source.get("ADMIN_IDS", "")
    return frozenset(int(x.strip()) for x in raw.split(",") if x.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    database_url: str | None
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
        return _MOSCOW_TIMEZONE if self.timezone_name == "Europe/Moscow" else ZoneInfo(self.timezone_name)


def load_settings(require_token: bool = True) -> Settings:
    return settings_from_mapping(os.environ, require_token=require_token)


def settings_from_mapping(source, require_token: bool = True) -> Settings:
    """Build settings from either local environment variables or Worker bindings."""
    token = source.get("BOT_TOKEN", "").strip()
    if require_token and not token:
        raise RuntimeError("BOT_TOKEN is not configured")
    db = Path(source.get("DATABASE_PATH", "./data/schedule_v3.db")).expanduser()
    return Settings(
        bot_token=token,
        admin_ids=_admin_ids(source),
        database_url=source.get("DATABASE_URL", "").strip() or None,
        timezone_name=source.get("TIMEZONE", "Europe/Moscow"),
        academic_year=source.get("ACADEMIC_YEAR", DEFAULT_ACADEMIC_YEAR),
        database_path=db,
        port=int(source.get("PORT", "10000")),
        source_sync_minutes=max(1, int(source.get("SOURCE_SYNC_MINUTES", "10"))),
        notification_tick_seconds=max(10, int(source.get("NOTIFICATION_TICK_SECONDS", "30"))),
        default_reminder_minutes=max(1, int(source.get("DEFAULT_REMINDER_MINUTES", "15"))),
        bootstrap_schedule_sources=_json_dict("BOOTSTRAP_SCHEDULE_SOURCES_JSON", source),
        bootstrap_replacement_sources=_json_dict("BOOTSTRAP_REPLACEMENT_SOURCES_JSON", source),
        bell_schedules=json.loads(
            source.get(
                "BELL_SCHEDULES_JSON",
                '{"main":[["08:15","09:00"],["09:15","10:00"],["10:15","11:00"],["11:15","12:00"],["12:15","13:00"],["13:15","14:00"],["14:15","15:00"],["15:15","16:00"]]}',
            )
        ),
        class_bell_schedules=_json_dict("CLASS_BELL_SCHEDULES_JSON", source),
    )
