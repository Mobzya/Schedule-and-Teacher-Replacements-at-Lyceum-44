from __future__ import annotations

import argparse
import asyncio
import sqlite3
from pathlib import Path

from app.config import load_settings
from app.db import Database


async def main(old: Path):
    settings = load_settings(require_token=False)
    db = Database(settings.database_path)
    await db.init()
    conn = sqlite3.connect(old)
    cur = conn.cursor()
    try:
        rows = cur.execute("SELECT user_id,class_name,profile,notify FROM users").fetchall()
    except sqlite3.Error as exc:
        raise SystemExit(f"Не удалось прочитать legacy users: {exc}")
    for uid, _old_class, _profile, notify in rows:
        # Настройки переносим, прошлогодний класс намеренно не переносим в текущий academic_year.
        await db.upsert_user(int(uid), settings.academic_year, default_reminder=settings.default_reminder_minutes)
        await db.update_user_setting(int(uid), "notifications", 1 if notify else 0)
    print(f"Migrated {len(rows)} users; class selection intentionally reset")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("old_db", type=Path)
    a = p.parse_args()
    asyncio.run(main(a.old_db))
