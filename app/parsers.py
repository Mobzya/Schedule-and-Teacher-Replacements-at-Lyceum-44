from __future__ import annotations

import csv
import io
import re
from datetime import date

from .domain import ParsedLesson, ParsedReplacement
from .utils import academic_year_for, normalize_class_name

WEEKDAY_NAMES = {
    "понедельник": 0,
    "пн": 0,
    "вторник": 1,
    "вт": 1,
    "среда": 2,
    "ср": 2,
    "четверг": 3,
    "чт": 3,
    "пятница": 4,
    "пт": 4,
    "суббота": 5,
    "сб": 5,
    "воскресенье": 6,
    "вс": 6,
}
ACADEMIC_YEAR_RE = re.compile(r"(20\d{2})\s*[-–—/]\s*(20\d{2})")
CLASS_RE = re.compile(r"^\s*(\d{1,2})\s*([а-яёa-z])\s*(?:\(([^)]+)\))?\s*$", re.I)
FULL_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](20\d{2})(?!\d)")
SHORT_DATE_RE = re.compile(r"^\s*(\d{1,2})[./](\d{1,2})\s*$")
TEXT_MONTH_DATE_RE = re.compile(r"^\s*(\d{1,2})[.\s/-]+([\wа-яё]+)\.?\s*$", re.I)
RU_MONTHS = {
    "янв": 1,
    "январь": 1,
    "фев": 2,
    "февраль": 2,
    "мар": 3,
    "март": 3,
    "апр": 4,
    "апрель": 4,
    "май": 5,
    "мая": 5,
    "июн": 6,
    "июнь": 6,
    "июл": 7,
    "июль": 7,
    "авг": 8,
    "август": 8,
    "сен": 9,
    "сент": 9,
    "сентябрь": 9,
    "окт": 10,
    "октябрь": 10,
    "ноя": 11,
    "ноябрь": 11,
    "дек": 12,
    "декабрь": 12,
}


def read_csv(text: str) -> list[list[str]]:
    text = text.lstrip("\ufeff")
    return [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text))]


def declared_academic_year(rows: list[list[str]]) -> str | None:
    for row in rows[:30]:
        joined = " ".join(row)
        m = ACADEMIC_YEAR_RE.search(joined)
        if m:
            return f"{m.group(1)}-{m.group(2)}"
    return None


def explicit_date_academic_years(text: str) -> set[str]:
    """Return academic years proven by full dates printed in a sheet."""
    years = set()
    for row in read_csv(text):
        for match in FULL_DATE_RE.finditer(" ".join(row)):
            try:
                parsed = date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
            except ValueError:
                continue
            years.add(academic_year_for(parsed))
    return years


def _class_columns(rows: list[list[str]]) -> tuple[int, list[tuple[int, str, str | None]]]:
    best: tuple[int, list[tuple[int, str, str | None]]] | None = None
    for i, row in enumerate(rows[:80]):
        found = []
        for j, cell in enumerate(row):
            m = CLASS_RE.match(cell)
            if not m:
                continue
            grade = int(m.group(1))
            if not 1 <= grade <= 11:
                continue
            class_name = normalize_class_name(f"{grade}{m.group(2)}")
            profile = m.group(3).strip() if m.group(3) else None
            found.append((j, class_name, profile))
        if len(found) >= 1 and (best is None or len(found) > len(best[1])):
            best = (i, found)
    if best is None:
        raise ValueError("Не удалось найти строку с названиями классов")
    return best


def _lesson_number(row: list[str]) -> int | None:
    # В реальных листах номер урока может дублироваться в нескольких блоках.
    for cell in row:
        value = cell.strip()
        if value.isdigit() and 1 <= int(value) <= 12:
            return int(value)
    return None


def _weekday_in_row(row: list[str]) -> int | None:
    for cell in row:
        key = cell.strip().lower().replace("ё", "е")
        if key in WEEKDAY_NAMES:
            return WEEKDAY_NAMES[key]
    return None


def _parse_tabular_schedule(rows: list[list[str]], lesson_date: date | None, weekly: bool) -> list[ParsedLesson]:
    header_idx = None
    for i, row in enumerate(rows[:100]):
        norm = [_norm_header(x) for x in row]
        if "класс" in norm and ("урок" in norm or "номер урока" in norm) and "предмет" in norm:
            if weekly and not any(x in norm for x in ("день", "день недели")):
                continue
            header_idx = i
            break
    if header_idx is None:
        return []
    headers = rows[header_idx]
    ci = _header_index(headers, ("класс",))
    li = _header_index(headers, ("урок", "номер урока"))
    si = _header_index(headers, ("предмет",))
    ri = _header_index(headers, ("кабинет", "каб"), False)
    pi = _header_index(headers, ("профиль",), False)
    di = _header_index(headers, ("день", "день недели"), False)
    result = []
    for row in rows[header_idx + 1 :]:
        if max(ci, li, si) >= len(row):
            continue
        if not row[li].strip().isdigit():
            continue
        num = int(row[li].strip())
        if not 1 <= num <= 12:
            continue
        class_name = normalize_class_name(row[ci])
        subject = row[si].strip()
        if not class_name or not subject:
            continue
        profile = row[pi].strip() if pi is not None and pi < len(row) and row[pi].strip() else None
        room = row[ri].strip() if ri is not None and ri < len(row) else ""
        weekday = None
        if weekly:
            if di is None or di >= len(row):
                continue
            key = row[di].strip().lower().replace("ё", "е")
            weekday = WEEKDAY_NAMES.get(key)
            if weekday is None:
                continue
        result.append(ParsedLesson(class_name, profile, weekday, lesson_date, num, subject, room))
    return _dedupe_lessons(result)


def parse_weekly_schedule(text: str) -> tuple[list[ParsedLesson], str | None]:
    rows = read_csv(text)
    year = declared_academic_year(rows)
    tabular = _parse_tabular_schedule(rows, None, True)
    if tabular:
        return tabular, year
    header_idx, classes = _class_columns(rows)
    lessons: list[ParsedLesson] = []
    current_weekday: int | None = None

    for row in rows[header_idx + 1 :]:
        wd = _weekday_in_row(row)
        if wd is not None:
            current_weekday = wd
        num = _lesson_number(row)
        if num is None or current_weekday is None:
            continue
        for col, class_name, profile in classes:
            if col >= len(row):
                continue
            subject = row[col].strip()
            if not subject or subject.lower().replace("ё", "е") in {"предмет", "каб", "кабинет", "переход"}:
                continue
            # Не принимаем повторные служебные ячейки за предмет.
            if subject.lower().replace("ё", "е") in WEEKDAY_NAMES or subject.isdigit():
                continue
            room = row[col + 1].strip() if col + 1 < len(row) else ""
            lessons.append(ParsedLesson(class_name, profile, current_weekday, None, num, subject, room))

    if len(lessons) < 5:
        raise ValueError(f"Слишком мало уроков после разбора: {len(lessons)}")
    return _dedupe_lessons(lessons), year


def _find_date(rows: list[list[str]]) -> date | None:
    for row in rows[:40]:
        joined = " ".join(row)
        m = FULL_DATE_RE.search(joined)
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None


def parse_date_schedule(text: str, forced_date: date | None = None) -> tuple[list[ParsedLesson], str | None, date]:
    rows = read_csv(text)
    year = declared_academic_year(rows)
    lesson_date = forced_date or _find_date(rows)
    if lesson_date is None:
        raise ValueError("Не удалось определить дату однодневного расписания; укажите её при добавлении источника")
    tabular = _parse_tabular_schedule(rows, lesson_date, False)
    if tabular:
        return tabular, year, lesson_date
    header_idx, classes = _class_columns(rows)
    lessons: list[ParsedLesson] = []
    for row in rows[header_idx + 1 :]:
        num = _lesson_number(row)
        if num is None:
            continue
        for col, class_name, profile in classes:
            if col >= len(row):
                continue
            subject = row[col].strip()
            if not subject or subject.lower() in {"предмет", "каб", "кабинет", "переход"} or subject.isdigit():
                continue
            if subject.lower().replace("ё", "е") in WEEKDAY_NAMES:
                continue
            room = row[col + 1].strip() if col + 1 < len(row) else ""
            lessons.append(ParsedLesson(class_name, profile, None, lesson_date, num, subject, room))
    if len(lessons) < 3:
        raise ValueError(f"Слишком мало уроков после разбора: {len(lessons)}")
    return _dedupe_lessons(lessons), year, lesson_date


def _norm_header(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower().replace("ё", "е"))


def _header_index(headers: list[str], aliases: tuple[str, ...], required: bool = True) -> int | None:
    normalized = [_norm_header(x) for x in headers]
    for alias in aliases:
        alias = _norm_header(alias)
        if alias in normalized:
            return normalized.index(alias)
    if required:
        raise ValueError(f"Не найдена колонка: {aliases[0]}")
    return None


def parse_replacements(text: str, fallback_year: int | None = None) -> tuple[list[ParsedReplacement], str | None]:
    rows = read_csv(text)
    year = declared_academic_year(rows)
    header_idx = None
    for i, row in enumerate(rows[:100]):
        normalized = {_norm_header(c) for c in row}
        if "дата" in normalized and ("урок" in normalized or "номер урока" in normalized) and "класс" in normalized:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("Не найдена шапка таблицы замен")
    headers = rows[header_idx]
    date_i = _header_index(headers, ("дата",))
    lesson_i = _header_index(headers, ("урок", "номер урока"))
    class_i = _header_index(headers, ("класс",))
    subject_i = _header_index(headers, ("предмет",), False)
    teacher_i = _header_index(headers, ("заменяющий учитель", "учитель", "замена"), False)
    room_i = _header_index(headers, ("кабинет", "каб"), False)
    note_i = _header_index(headers, ("примеч", "примечание", "комментарий"), False)

    month_year: dict[int, int] = {}
    for row in rows:
        joined = " ".join(row)
        m = FULL_DATE_RE.search(joined)
        if m:
            month_year[int(m.group(2))] = int(m.group(3))

    replacements: list[ParsedReplacement] = []
    for row in rows[header_idx + 1 :]:
        if max(date_i, lesson_i, class_i) >= len(row):
            continue
        raw_date = row[date_i].strip()
        raw_lesson = row[lesson_i].strip()
        class_name = normalize_class_name(row[class_i])
        if not raw_date or not raw_lesson.isdigit() or not class_name:
            continue
        lesson_num = int(raw_lesson)
        if not 1 <= lesson_num <= 12:
            continue
        parsed_date = None
        m = FULL_DATE_RE.search(raw_date)
        if m:
            try:
                parsed_date = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                parsed_date = None
        else:
            sm = SHORT_DATE_RE.match(raw_date)
            tm = TEXT_MONTH_DATE_RE.match(raw_date)
            day = month = None
            if sm:
                day, month = int(sm.group(1)), int(sm.group(2))
            elif tm:
                day = int(tm.group(1))
                month = RU_MONTHS.get(tm.group(2).lower().replace("ё", "е"))
            if day is not None and month is not None:
                y = month_year.get(month) or (
                    (fallback_year + 1) if fallback_year is not None and month <= 7 else fallback_year
                )
                if y:
                    try:
                        parsed_date = date(y, month, day)
                    except ValueError:
                        parsed_date = None
        if parsed_date is None:
            continue
        subject = row[subject_i].strip() if subject_i is not None and subject_i < len(row) else None
        teacher = row[teacher_i].strip() if teacher_i is not None and teacher_i < len(row) else None
        room = row[room_i].strip() if room_i is not None and room_i < len(row) else None
        note = row[note_i].strip() if note_i is not None and note_i < len(row) else None
        combined = " ".join(x or "" for x in (subject, teacher, room, note)).lower().replace("ё", "е")
        cancelled = any(token in combined for token in ("отмена", "отменен", "отменён", "урока не будет"))
        replacements.append(
            ParsedReplacement(
                parsed_date,
                class_name,
                lesson_num,
                subject or None,
                teacher or None,
                room or None,
                cancelled,
                note or None,
            )
        )
    return _dedupe_replacements(replacements), year


def _dedupe_lessons(items: list[ParsedLesson]) -> list[ParsedLesson]:
    result = {}
    for x in items:
        key = (x.class_name, x.profile, x.weekday, x.lesson_date, x.lesson_number)
        result[key] = x
    return list(result.values())


def _dedupe_replacements(items: list[ParsedReplacement]) -> list[ParsedReplacement]:
    result = {}
    for x in items:
        result[(x.lesson_date, x.class_name, x.lesson_number)] = x
    return list(result.values())
