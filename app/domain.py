from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from enum import StrEnum


class SourceType(StrEnum):
    WEEKLY = "weekly_schedule"
    DATE = "date_schedule"
    REPLACEMENTS = "replacements"


class PublicationState(StrEnum):
    WAITING = "waiting"
    READY = "ready"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class ParsedLesson:
    class_name: str
    profile: str | None
    weekday: int | None
    lesson_date: date | None
    lesson_number: int
    subject: str
    room: str
    teacher: str | None = None
    start_time: time | None = None
    end_time: time | None = None


@dataclass(frozen=True, slots=True)
class ParsedReplacement:
    lesson_date: date
    class_name: str
    lesson_number: int
    subject: str | None
    teacher: str | None
    room: str | None
    cancelled: bool = False
    note: str | None = None


@dataclass(frozen=True, slots=True)
class EffectiveLesson:
    lesson_number: int
    subject: str
    room: str
    teacher: str | None
    start_time: time | None
    end_time: time | None
    changed: bool = False
    cancelled: bool = False
