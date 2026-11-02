from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    import aiosqlite
except ImportError:  # Cloudflare Workers use the D1 binding instead.
    aiosqlite = None

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # psycopg is not available in the Workers runtime.
    psycopg = None
    dict_row = None

from .domain import ParsedLesson, ParsedReplacement, PublicationState, SourceType

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY,
  class_name TEXT,
  profile TEXT,
  academic_year TEXT,
  notifications INTEGER NOT NULL DEFAULT 1,
  reminder_minutes INTEGER NOT NULL DEFAULT 15,
  replacement_notifications INTEGER NOT NULL DEFAULT 1,
  morning_summary INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS data_sources(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  source_type TEXT NOT NULL,
  url TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  priority INTEGER NOT NULL DEFAULT 100,
  valid_from TEXT,
  valid_until TEXT,
  expected_academic_year TEXT,
  last_status TEXT NOT NULL DEFAULT 'never',
  last_error TEXT,
  last_success_at TEXT,
  last_checked_at TEXT,
  content_hash TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS weekly_lessons(
  source_id INTEGER NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
  class_name TEXT NOT NULL,
  profile TEXT,
  weekday INTEGER NOT NULL,
  lesson_number INTEGER NOT NULL,
  subject TEXT NOT NULL,
  room TEXT NOT NULL DEFAULT '',
  teacher TEXT,
  start_time TEXT,
  end_time TEXT,
  PRIMARY KEY(source_id,class_name,profile,weekday,lesson_number)
);
CREATE TABLE IF NOT EXISTS dated_lessons(
  source_id INTEGER NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
  lesson_date TEXT NOT NULL,
  class_name TEXT NOT NULL,
  profile TEXT,
  lesson_number INTEGER NOT NULL,
  subject TEXT NOT NULL,
  room TEXT NOT NULL DEFAULT '',
  teacher TEXT,
  start_time TEXT,
  end_time TEXT,
  PRIMARY KEY(source_id,lesson_date,class_name,profile,lesson_number)
);
CREATE TABLE IF NOT EXISTS replacements(
  source_id INTEGER NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
  lesson_date TEXT NOT NULL,
  class_name TEXT NOT NULL,
  lesson_number INTEGER NOT NULL,
  subject TEXT,
  teacher TEXT,
  room TEXT,
  cancelled INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  PRIMARY KEY(source_id,lesson_date,class_name,lesson_number)
);
CREATE TABLE IF NOT EXISTS school_calendar(
  day TEXT PRIMARY KEY,
  is_school_day INTEGER NOT NULL,
  schedule_weekday INTEGER,
  note TEXT,
  updated_by INTEGER,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_state(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_log(
  user_id INTEGER NOT NULL,
  notification_key TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  PRIMARY KEY(user_id, notification_key)
);
CREATE TABLE IF NOT EXISTS notification_outbox(
  user_id INTEGER NOT NULL,
  notification_key TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  last_error TEXT,
  PRIMARY KEY(user_id, notification_key)
);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_id INTEGER,
  action TEXT NOT NULL,
  payload TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dated_lookup ON dated_lessons(lesson_date,class_name);
CREATE INDEX IF NOT EXISTS idx_weekly_lookup ON weekly_lessons(weekday,class_name);
CREATE INDEX IF NOT EXISTS idx_replacements_lookup ON replacements(lesson_date,class_name);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_due ON notification_outbox(next_attempt_at,created_at);
CREATE TABLE IF NOT EXISTS friends (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  label_key TEXT NOT NULL,
  class_name TEXT NOT NULL,
  profile TEXT,
  academic_year TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(owner_id, label_key)
);
"""

POSTGRES_SCHEMA = (
    SCHEMA.replace("PRAGMA foreign_keys=ON;", "")
    .replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    .replace(
        "PRIMARY KEY(source_id,class_name,profile,weekday,lesson_number)",
        "UNIQUE NULLS NOT DISTINCT(source_id,class_name,profile,weekday,lesson_number)",
    )
    .replace(
        "PRIMARY KEY(source_id,lesson_date,class_name,profile,lesson_number)",
        "UNIQUE NULLS NOT DISTINCT(source_id,lesson_date,class_name,profile,lesson_number)",
    )
)


class PostgresConnection:
    """Small compatibility layer that keeps the repository's SQL backend-neutral."""

    def __init__(self, connection: psycopg.AsyncConnection):
        self.connection = connection

    @staticmethod
    def _sql(query: str) -> str:
        return query.replace("?", "%s")

    async def execute(self, query: str, params: tuple = ()):
        return await self.connection.execute(self._sql(query), params)

    async def executemany(self, query: str, params: Iterable[tuple]):
        cursor = self.connection.cursor()
        await cursor.executemany(self._sql(query), params)
        return cursor

    async def commit(self) -> None:
        await self.connection.commit()

    async def rollback(self) -> None:
        await self.connection.rollback()


class D1Cursor:
    def __init__(self, results: list[dict], lastrowid: int | None = None, rowcount: int = 0):
        self._results = results
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    async def fetchone(self):
        return self._results[0] if self._results else None

    async def fetchall(self):
        return self._results


class D1Connection:
    """Async adapter for the Cloudflare D1 prepared-statement API."""

    def __init__(self, binding):
        self.binding = binding
        self.pending: list[tuple[str, tuple]] | None = None

    async def execute(self, query: str, params: tuple = ()) -> D1Cursor:
        if query.strip().upper().startswith("BEGIN"):
            self.pending = []
            return D1Cursor([])
        if query.strip().upper() in {"COMMIT", "ROLLBACK"}:
            if query.strip().upper() == "COMMIT" and self.pending:
                statements = [self._statement(sql, values) for sql, values in self.pending]
                await self.binding.batch(statements)
            self.pending = None
            return D1Cursor([])
        if self.pending is not None:
            self.pending.append((query, tuple(params)))
            return D1Cursor([])
        statement = self._statement(query, tuple(params))
        result = await statement.all()
        rows = list(getattr(result, "results", []) or [])
        meta = getattr(result, "meta", None)
        return D1Cursor(
            [dict(row) for row in rows],
            getattr(meta, "last_row_id", None) if meta else None,
            int(getattr(meta, "changes", 0) or 0) if meta else 0,
        )

    async def executemany(self, query: str, params: Iterable[tuple]) -> D1Cursor:
        if self.pending is None:
            raise RuntimeError("D1 executemany requires a transaction")
        rows = [tuple(values) for values in params]
        if not rows:
            return D1Cursor([])
        values_clause = re.search(r"VALUES\s*\((?:\s*\?\s*,)*\s*\?\s*\)\s*$", query, re.IGNORECASE)
        if values_clause:
            # A whole snapshot becomes one native D1 INSERT instead of hundreds
            # of Python -> JS prepared-statement conversions. DELETE + INSERT
            # still share the same atomic batch, so a failed import keeps old data.
            count = query[values_clause.start() :].count("?")
            if any(len(row) != count for row in rows):
                raise ValueError("D1 bulk insert parameter count mismatch")
            columns = ",".join(f"json_extract(value, '$[{index}]')" for index in range(count))
            sql = query[: values_clause.start()] + f"SELECT {columns} FROM json_each(?)"
            self.pending.append((sql, (json.dumps(rows, ensure_ascii=False),)))
        else:
            self.pending.extend((query, values) for values in rows)
        return D1Cursor([])

    async def commit(self) -> None:
        if self.pending:
            statements = [self._statement(sql, values) for sql, values in self.pending]
            await self.binding.batch(statements)
        self.pending = None

    async def rollback(self) -> None:
        self.pending = None

    def _statement(self, query: str, params: tuple):
        statement = self.binding.prepare(query)
        return statement.bind(*params) if params else statement


class Database:
    def __init__(self, path: Path, database_url: str | None = None, d1_binding=None):
        self.path = path
        self.database_url = database_url
        self.d1_binding = d1_binding
        if database_url and not database_url.startswith(("postgres://", "postgresql://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection string")

    async def init(self) -> None:
        if self.d1_binding is not None:
            return  # Schema is managed by Wrangler migrations.
        if self.database_url:
            if psycopg is None:
                raise RuntimeError("psycopg is required for PostgreSQL")
            connection = await psycopg.AsyncConnection.connect(self.database_url, row_factory=dict_row)
            try:
                for statement in POSTGRES_SCHEMA.split(";"):
                    if statement.strip():
                        await connection.execute(statement)
                await connection.commit()
            finally:
                await connection.close()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if aiosqlite is None:
            raise RuntimeError("aiosqlite is required for SQLite")
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await db.execute("PRAGMA journal_mode=WAL")
            await db.commit()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[Any]:
        if self.d1_binding is not None:
            yield D1Connection(self.d1_binding)
            return
        if self.database_url:
            if psycopg is None:
                raise RuntimeError("psycopg is required for PostgreSQL")
            connection = await psycopg.AsyncConnection.connect(self.database_url, row_factory=dict_row)
            try:
                yield PostgresConnection(connection)
            finally:
                await connection.close()
            return
        if aiosqlite is None:
            raise RuntimeError("aiosqlite is required for SQLite")
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            await db.close()

    async def get_state(self, key: str, default: str | None = None) -> str | None:
        async with self.connect() as db:
            cur = await db.execute("SELECT value FROM app_state WHERE key=?", (key,))
            row = await cur.fetchone()
            return row["value"] if row else default

    async def set_state(self, key: str, value: str) -> None:
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO app_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            await db.commit()

    async def publication_state(self) -> PublicationState:
        value = await self.get_state("publication_state", PublicationState.WAITING.value)
        try:
            return PublicationState(value)
        except ValueError:
            return PublicationState.WAITING

    async def set_publication_state(self, state: PublicationState, actor_id: int | None = None) -> None:
        await self.set_state("publication_state", state.value)
        await self.audit(actor_id, "publication_state", {"state": state.value})

    async def waiting_message(self) -> str:
        return (
            await self.get_state(
                "waiting_message",
                "Расписание на новый учебный год ещё формируется. Как только его опубликуют, оно появится здесь автоматически.",
            )
            or ""
        )

    async def set_waiting_message(self, text: str, actor_id: int | None = None) -> None:
        await self.set_state("waiting_message", text)
        await self.audit(actor_id, "waiting_message", {"text": text})

    async def audit(self, actor_id: int | None, action: str, payload: dict[str, Any] | None = None) -> None:
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO audit_log(actor_id,action,payload,created_at) VALUES(?,?,?,?)",
                (actor_id, action, json.dumps(payload or {}, ensure_ascii=False), datetime.now(UTC).isoformat()),
            )
            await db.commit()

    async def upsert_user(
        self,
        user_id: int,
        academic_year: str,
        class_name: str | None = None,
        profile: str | None = None,
        default_reminder: int = 15,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        async with self.connect() as db:
            await db.execute(
                """INSERT INTO users(user_id,class_name,profile,academic_year,reminder_minutes,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     class_name=CASE
                       WHEN users.academic_year IS DISTINCT FROM excluded.academic_year THEN excluded.class_name
                       ELSE COALESCE(excluded.class_name,users.class_name)
                     END,
                     profile=CASE
                       WHEN users.academic_year IS DISTINCT FROM excluded.academic_year
                         OR excluded.class_name IS NOT NULL THEN excluded.profile
                       ELSE users.profile
                     END,
                     academic_year=excluded.academic_year,
                     updated_at=excluded.updated_at""",
                (user_id, class_name, profile, academic_year, default_reminder, now, now),
            )
            await db.commit()

    async def get_user(self, user_id: int):
        async with self.connect() as db:
            cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
            return await cur.fetchone()

    async def set_user_class(
        self, user_id: int, class_name: str, profile: str | None, academic_year: str, default_reminder: int
    ) -> None:
        await self.upsert_user(user_id, academic_year, class_name, profile, default_reminder)

    async def update_user_setting(self, user_id: int, field: str, value: int) -> None:
        allowed = {"notifications", "reminder_minutes", "replacement_notifications", "morning_summary"}
        if field not in allowed:
            raise ValueError("Unsupported setting")
        async with self.connect() as db:
            await db.execute(
                f"UPDATE users SET {field}=?, updated_at=? WHERE user_id=?",
                (value, datetime.now(UTC).isoformat(), user_id),
            )
            await db.commit()

    async def all_users(
        self, after_user_id: int | None = None, limit: int | None = None, notification_year: str | None = None
    ):
        async with self.connect() as db:
            query = "SELECT * FROM users"
            params: tuple = ()
            where = []
            if notification_year is not None:
                where.append(
                    "academic_year=? AND class_name IS NOT NULL AND class_name<>'' "
                    "AND (notifications=1 OR replacement_notifications=1 OR morning_summary=1)"
                )
                params += (notification_year,)
            if after_user_id is not None:
                where.append("user_id>?")
                params += (after_user_id,)
            if where:
                query += " WHERE " + " AND ".join(where)
            query += " ORDER BY user_id"
            if limit is not None:
                query += " LIMIT ?"
                params += (limit,)
            cur = await db.execute(query, params)
            return await cur.fetchall()

    async def list_friends(self, owner_id: int):
        async with self.connect() as db:
            cur = await db.execute("SELECT * FROM friends WHERE owner_id=? ORDER BY id LIMIT 30", (owner_id,))
            return await cur.fetchall()

    async def get_friend(self, owner_id: int, friend_id: int):
        async with self.connect() as db:
            cur = await db.execute("SELECT * FROM friends WHERE owner_id=? AND id=?", (owner_id, friend_id))
            return await cur.fetchone()

    async def save_friend(
        self,
        owner_id: int,
        label: str,
        class_name: str,
        profile: str | None,
        academic_year: str,
        friend_id: int | None = None,
    ) -> bool:
        """Atomic per-owner limit/duplicate checks, including concurrent webhook requests."""
        now = datetime.now(UTC).isoformat()
        key = label.casefold()
        async with self.connect() as db:
            if friend_id is None:
                cur = await db.execute(
                    """INSERT INTO friends(owner_id,label,label_key,class_name,profile,academic_year,created_at,updated_at)
                       SELECT ?,?,?,?,?,?,?,? WHERE (SELECT COUNT(*) FROM friends WHERE owner_id=?)<30
                       ON CONFLICT(owner_id,label_key) DO NOTHING RETURNING id""",
                    (owner_id, label, key, class_name, profile, academic_year, now, now, owner_id),
                )
            else:
                cur = await db.execute(
                    """UPDATE friends SET label=?,label_key=?,class_name=?,profile=?,academic_year=?,updated_at=?
                       WHERE owner_id=? AND id=? AND NOT EXISTS (
                         SELECT 1 FROM friends WHERE owner_id=? AND label_key=? AND id<>?
                       ) RETURNING id""",
                    (
                        label,
                        key,
                        class_name,
                        profile,
                        academic_year,
                        now,
                        owner_id,
                        friend_id,
                        owner_id,
                        key,
                        friend_id,
                    ),
                )
            saved = await cur.fetchone()
            await db.commit()
            return saved is not None

    async def delete_friend(self, owner_id: int, friend_id: int) -> None:
        async with self.connect() as db:
            await db.execute("DELETE FROM friends WHERE owner_id=? AND id=?", (owner_id, friend_id))
            await db.commit()

    async def add_source(
        self,
        name: str,
        source_type: SourceType,
        url: str,
        priority: int,
        valid_from: date | None,
        valid_until: date | None,
        expected_year: str | None,
        created_by: int | None,
    ) -> int:
        async with self.connect() as db:
            query = """INSERT INTO data_sources(name,source_type,url,priority,valid_from,valid_until,expected_academic_year,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)"""
            if self.database_url or self.d1_binding is not None:
                query += " RETURNING id"
            cur = await db.execute(
                query,
                (
                    name,
                    source_type.value,
                    url,
                    priority,
                    valid_from.isoformat() if valid_from else None,
                    valid_until.isoformat() if valid_until else None,
                    expected_year,
                    created_by,
                    datetime.now(UTC).isoformat(),
                ),
            )
            source_id = (
                int((await cur.fetchone())["id"])
                if self.database_url or self.d1_binding is not None
                else int(cur.lastrowid)
            )
            await db.commit()
            return source_id

    async def bootstrap_source(
        self, name: str, source_type: SourceType, url: str, expected_year: str | None, priority: int = 10
    ) -> None:
        async with self.connect() as db:
            await db.execute(
                """INSERT INTO data_sources(name,source_type,url,priority,expected_academic_year,created_at)
                   SELECT ?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM data_sources WHERE name=?)
                   ON CONFLICT(name) DO NOTHING""",
                (name, source_type.value, url, priority, expected_year, datetime.now(UTC).isoformat(), name),
            )
            await db.commit()

    async def list_sources(self, enabled_only: bool = False):
        where = "WHERE enabled=1" if enabled_only else ""
        async with self.connect() as db:
            cur = await db.execute(f"SELECT * FROM data_sources {where} ORDER BY source_type, priority DESC, id")
            return await cur.fetchall()

    async def get_source(self, source_id: int):
        async with self.connect() as db:
            cur = await db.execute("SELECT * FROM data_sources WHERE id=?", (source_id,))
            return await cur.fetchone()

    async def migrate_source_url(self, source_id: int, old_url: str, new_url: str) -> None:
        """Update a known obsolete URL without deleting the last good snapshot."""
        async with self.connect() as db:
            await db.execute(
                """UPDATE data_sources SET url=?,content_hash=NULL,last_status='never',last_error=NULL
                   WHERE id=? AND url=?""",
                (new_url, source_id, old_url),
            )
            await db.commit()

    async def replacement_summary(self) -> dict[str, Any]:
        async with self.connect() as db:
            cur = await db.execute(
                """SELECT COUNT(*) AS count,MIN(r.lesson_date) AS first_date,MAX(r.lesson_date) AS last_date
                   FROM replacements r JOIN data_sources ds ON ds.id=r.source_id WHERE ds.enabled=1"""
            )
            return dict(await cur.fetchone())

    async def set_source_enabled(self, source_id: int, enabled: bool, actor_id: int | None = None) -> None:
        async with self.connect() as db:
            await db.execute("UPDATE data_sources SET enabled=? WHERE id=?", (1 if enabled else 0, source_id))
            await db.commit()
        await self.audit(actor_id, "source_enabled", {"source_id": source_id, "enabled": enabled})

    async def disable_sources_by_url_fragment(self, fragment: str, reason: str) -> int:
        """Disable a known unsafe legacy source while preserving it for inspection."""
        async with self.connect() as db:
            cur = await db.execute(
                """UPDATE data_sources SET enabled=0,last_status='stale',last_error=?
                   WHERE enabled=1 AND url LIKE ?""",
                (reason, f"%{fragment}%"),
            )
            await db.commit()
            return max(0, int(cur.rowcount))

    async def delete_source(self, source_id: int, actor_id: int | None = None) -> None:
        async with self.connect() as db:
            await db.execute("DELETE FROM data_sources WHERE id=?", (source_id,))
            await db.commit()
        await self.audit(actor_id, "source_deleted", {"source_id": source_id})

    async def source_status(
        self, source_id: int, status: str, error: str | None, content_hash: str | None = None, success: bool = False
    ) -> None:
        now = datetime.now(UTC).isoformat()
        async with self.connect() as db:
            await db.execute(
                """UPDATE data_sources SET last_status=?,last_error=?,last_checked_at=?,content_hash=COALESCE(?,content_hash),
                   last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END WHERE id=?""",
                (status, error, now, content_hash, success, now, source_id),
            )
            await db.commit()

    async def replace_source_snapshot(
        self,
        source_id: int,
        source_type: SourceType,
        lessons: Iterable[ParsedLesson],
        replacements: Iterable[ParsedReplacement],
    ) -> None:
        async with self.connect() as db:
            await db.execute("BEGIN" if self.database_url or self.d1_binding is not None else "BEGIN IMMEDIATE")
            try:
                if source_type == SourceType.WEEKLY:
                    await db.execute("DELETE FROM weekly_lessons WHERE source_id=?", (source_id,))
                    await db.executemany(
                        """INSERT INTO weekly_lessons(source_id,class_name,profile,weekday,lesson_number,subject,room,teacher,start_time,end_time)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        [
                            (
                                source_id,
                                lesson.class_name,
                                lesson.profile,
                                lesson.weekday,
                                lesson.lesson_number,
                                lesson.subject,
                                lesson.room,
                                lesson.teacher,
                                lesson.start_time.isoformat() if lesson.start_time else None,
                                lesson.end_time.isoformat() if lesson.end_time else None,
                            )
                            for lesson in lessons
                        ],
                    )
                elif source_type == SourceType.DATE:
                    await db.execute("DELETE FROM dated_lessons WHERE source_id=?", (source_id,))
                    await db.executemany(
                        """INSERT INTO dated_lessons(source_id,lesson_date,class_name,profile,lesson_number,subject,room,teacher,start_time,end_time)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        [
                            (
                                source_id,
                                lesson.lesson_date.isoformat(),
                                lesson.class_name,
                                lesson.profile,
                                lesson.lesson_number,
                                lesson.subject,
                                lesson.room,
                                lesson.teacher,
                                lesson.start_time.isoformat() if lesson.start_time else None,
                                lesson.end_time.isoformat() if lesson.end_time else None,
                            )
                            for lesson in lessons
                        ],
                    )
                else:
                    await db.execute("DELETE FROM replacements WHERE source_id=?", (source_id,))
                    await db.executemany(
                        """INSERT INTO replacements(source_id,lesson_date,class_name,lesson_number,subject,teacher,room,cancelled,note)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        [
                            (
                                source_id,
                                r.lesson_date.isoformat(),
                                r.class_name,
                                r.lesson_number,
                                r.subject,
                                r.teacher,
                                r.room,
                                1 if r.cancelled else 0,
                                r.note,
                            )
                            for r in replacements
                        ],
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def classes(self) -> list[tuple[str, str | None]]:
        async with self.connect() as db:
            cur = await db.execute("""SELECT DISTINCT wl.class_name,wl.profile
                                      FROM weekly_lessons wl JOIN data_sources ds ON ds.id=wl.source_id
                                      WHERE ds.enabled=1
                                    UNION SELECT DISTINCT dl.class_name,dl.profile
                                      FROM dated_lessons dl JOIN data_sources ds ON ds.id=dl.source_id
                                      WHERE ds.enabled=1
                                    ORDER BY class_name,profile""")
            return [(r["class_name"], r["profile"]) for r in await cur.fetchall()]

    async def calendar_override(self, day: date):
        async with self.connect() as db:
            cur = await db.execute("SELECT * FROM school_calendar WHERE day=?", (day.isoformat(),))
            return await cur.fetchone()

    async def set_calendar_day(
        self, day: date, is_school_day: bool, schedule_weekday: int | None, note: str | None, actor_id: int | None
    ):
        async with self.connect() as db:
            await db.execute(
                """INSERT INTO school_calendar(day,is_school_day,schedule_weekday,note,updated_by,updated_at)
                              VALUES(?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET is_school_day=excluded.is_school_day,
                              schedule_weekday=excluded.schedule_weekday,note=excluded.note,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                (
                    day.isoformat(),
                    1 if is_school_day else 0,
                    schedule_weekday,
                    note,
                    actor_id,
                    datetime.now(UTC).isoformat(),
                ),
            )
            await db.commit()

    async def clear_calendar_day(self, day: date):
        async with self.connect() as db:
            await db.execute("DELETE FROM school_calendar WHERE day=?", (day.isoformat(),))
            await db.commit()

    async def notification_sent(self, user_id: int, key: str) -> bool:
        async with self.connect() as db:
            cur = await db.execute(
                "SELECT 1 FROM notification_log WHERE user_id=? AND notification_key=?", (user_id, key)
            )
            return await cur.fetchone() is not None

    async def mark_notification(self, user_id: int, key: str) -> None:
        async with self.connect() as db:
            await db.execute(
                """INSERT INTO notification_log(user_id,notification_key,sent_at) VALUES(?,?,?)
                   ON CONFLICT(user_id,notification_key) DO NOTHING""",
                (user_id, key, datetime.now(UTC).isoformat()),
            )
            await db.commit()

    async def enqueue_notification(self, user_id: int, key: str, message: str) -> None:
        now = datetime.now(UTC).isoformat()
        async with self.connect() as db:
            await db.execute(
                """INSERT INTO notification_outbox(user_id,notification_key,message,created_at,next_attempt_at)
                   SELECT ?,?,?,?,? WHERE NOT EXISTS (
                     SELECT 1 FROM notification_log WHERE user_id=? AND notification_key=?
                   ) ON CONFLICT(user_id,notification_key) DO NOTHING""",
                (user_id, key, message, now, now, user_id, key),
            )
            await db.commit()

    async def enqueue_notifications(self, user_ids: Iterable[int], key: str, message: str) -> int:
        """Durably enqueue one announcement for a page of users in one DB batch."""
        now = datetime.now(UTC).isoformat()
        rows = [(int(user_id), key, message, now, now) for user_id in user_ids]
        if not rows:
            return 0
        async with self.connect() as db:
            await db.execute("BEGIN" if self.database_url or self.d1_binding is not None else "BEGIN IMMEDIATE")
            await db.executemany(
                """INSERT INTO notification_outbox(user_id,notification_key,message,created_at,next_attempt_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(user_id,notification_key) DO NOTHING""",
                rows,
            )
            await db.commit()
        return len(rows)

    async def pending_notifications(self, limit: int = 1):
        now = datetime.now(UTC).isoformat()
        async with self.connect() as db:
            cur = await db.execute(
                """SELECT * FROM notification_outbox WHERE next_attempt_at<=?
                   ORDER BY created_at,user_id LIMIT ?""",
                (now, limit),
            )
            return await cur.fetchall()

    async def finish_notification(self, user_id: int, key: str) -> None:
        async with self.connect() as db:
            await db.execute("BEGIN" if self.database_url or self.d1_binding is not None else "BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "INSERT INTO notification_log(user_id,notification_key,sent_at) VALUES(?,?,?) "
                    "ON CONFLICT(user_id,notification_key) DO NOTHING",
                    (user_id, key, datetime.now(UTC).isoformat()),
                )
                await db.execute(
                    "DELETE FROM notification_outbox WHERE user_id=? AND notification_key=?", (user_id, key)
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def retry_notification(self, user_id: int, key: str, error: str, delay_seconds: int) -> None:
        now = datetime.now(UTC)
        async with self.connect() as db:
            await db.execute(
                """UPDATE notification_outbox SET attempts=attempts+1,last_error=?,next_attempt_at=?
                   WHERE user_id=? AND notification_key=?""",
                (
                    error[:500],
                    (now + timedelta(seconds=max(1, delay_seconds))).isoformat(),
                    user_id,
                    key,
                ),
            )
            await db.commit()

    async def discard_notification(self, user_id: int, key: str) -> None:
        async with self.connect() as db:
            await db.execute("DELETE FROM notification_outbox WHERE user_id=? AND notification_key=?", (user_id, key))
            await db.commit()
