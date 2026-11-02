"""Idempotent SQLite/PostgreSQL -> D1 user migration. Dry-run by default.

No token, Telegram requests or broadcasts. Existing D1 users are never updated.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from app.config import DEFAULT_ACADEMIC_YEAR


def read_users(sqlite_path: Path | None, env_file: Path | None) -> list[dict]:
    if sqlite_path:
        with sqlite3.connect(sqlite_path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM users")]
    import psycopg
    from dotenv import dotenv_values
    from psycopg.rows import dict_row

    url = dotenv_values(env_file).get("DATABASE_URL")
    if not url:
        raise ValueError("В указанном env-файле нет DATABASE_URL")
    with psycopg.connect(url, connect_timeout=15, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        return list(connection.execute("SELECT * FROM users").fetchall())


def normalize_users(rows: list[dict], academic_year: str, classes: set[tuple]) -> list[dict]:
    now = datetime.now(UTC).isoformat()
    result = {}
    for row in rows:
        uid = int(row["user_id"])
        if not 0 < uid < 2**53:
            raise ValueError("Некорректный Telegram ID в исходной базе")
        pair = (row.get("class_name"), row.get("profile") or None)
        # Never silently promote a class or reuse last year's class selection.
        keep_class = row.get("academic_year") == academic_year and pair in classes
        minutes = int(row.get("reminder_minutes") or 15)
        result[uid] = {
            "user_id": uid,
            "class_name": pair[0] if keep_class else None,
            "profile": pair[1] if keep_class else None,
            "academic_year": academic_year,
            "notifications": int(bool(int(row.get("notifications", row.get("notify", 1))))),
            "reminder_minutes": minutes if minutes in (5, 10, 15, 20, 30, 45) else 15,
            "replacement_notifications": int(bool(int(row.get("replacement_notifications", 1)))),
            "morning_summary": int(bool(int(row.get("morning_summary", 0)))),
            "created_at": str(row.get("created_at") or now),
            "updated_at": now,
        }
    return list(result.values())


def literal(value) -> str:
    return "'" + json.dumps(value, ensure_ascii=False).replace("'", "''") + "'"


def import_sql(users: list[dict]) -> str:
    columns = (
        "user_id",
        "class_name",
        "profile",
        "academic_year",
        "notifications",
        "reminder_minutes",
        "replacement_notifications",
        "morning_summary",
        "created_at",
        "updated_at",
    )
    fields = ",".join(f"json_extract(value,'$.{key}')" for key in columns)
    return (
        f"INSERT INTO users({','.join(columns)}) SELECT {fields} FROM json_each({literal(users)}) "
        "WHERE 1 ON CONFLICT(user_id) DO NOTHING;"
    )


def remote(sql: str, database: str, write: bool = False):
    # A private temporary file avoids putting user IDs in command-line arguments
    # or shell history. Capture Wrangler output: display aggregate counts only.
    command = ["npx", "wrangler", "d1", "execute", database, "--remote", "--yes", "--json"]
    if write:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", encoding="utf-8") as temporary:
            temporary.write(sql)
            temporary.flush()
            process = subprocess.run([*command, "--file", temporary.name], capture_output=True, text=True, check=False)
    else:
        # Read queries contain no user IDs/credentials. --file uses D1 bulk
        # import and discards SELECT results, even with --json.
        process = subprocess.run([*command, "--command", sql], capture_output=True, text=True, check=False)
    if process.returncode:
        raise RuntimeError("Запрос к D1 не выполнен. Проверьте wrangler login и название базы; данные не выведены.")
    return [] if write else json.loads(process.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sqlite", type=Path)
    source.add_argument("--env-file", type=Path, help="Файл с DATABASE_URL старого PostgreSQL")
    parser.add_argument("--database", default="lyceum44-schedule")
    parser.add_argument("--academic-year", default=DEFAULT_ACADEMIC_YEAR)
    parser.add_argument("--apply", action="store_true", help="Добавить отсутствующих пользователей (без перезаписи)")
    args = parser.parse_args()
    try:
        rows = read_users(args.sqlite, args.env_file)
        available = remote(
            "SELECT DISTINCT class_name,profile FROM weekly_lessons w JOIN data_sources d ON w.source_id=d.id WHERE d.enabled=1 "
            "UNION SELECT DISTINCT class_name,profile FROM dated_lessons l JOIN data_sources d ON l.source_id=d.id WHERE d.enabled=1;",
            args.database,
        )
        classes = {(r["class_name"], r["profile"]) for r in available[0]["results"]}
        users = normalize_users(rows, args.academic_year, classes)
        ids = {row["user_id"] for row in users}

        def overlap():
            existing = {r["user_id"] for r in remote("SELECT user_id FROM users", args.database)[0]["results"]}
            return len(existing & ids)

        before = overlap()
        print(f"В исходной базе: {len(users)}. Уже есть в D1: {before}. Недостающих: {len(users) - before}.")
        print(
            f"Актуальный класс сохранится у {sum(bool(u['class_name']) for u in users)} исходных записей; остальные выберут класс заново."
        )
        if not args.apply:
            print("Проверка без изменений. Для импорта добавьте --apply.")
            return
        remote(import_sql(users), args.database, write=True)
        after = overlap()
        if after != len(users):
            raise RuntimeError("Проверка импорта не пройдена. Повторный запуск безопасен.")
        print(f"Подтверждено в D1: {after}/{len(users)}. Существующие пользователи и настройки не изменены.")
    except Exception as exc:
        # Connection exceptions may contain credentials: do not print them.
        raise SystemExit(
            f"Импорт остановлен ({type(exc).__name__}). Проверьте доступ к исходной БД и D1. Секреты не выведены."
        ) from None


if __name__ == "__main__":
    main()
