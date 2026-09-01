import sqlite3
import sys
import types

try:
    import aiosqlite  # noqa
except ImportError:
    mod = types.ModuleType("aiosqlite")
    mod.Row = sqlite3.Row

    class Cursor:
        def __init__(self, c):
            self._c = c
            self.lastrowid = c.lastrowid

        async def fetchone(self):
            return self._c.fetchone()

        async def fetchall(self):
            return self._c.fetchall()

    class Conn:
        def __init__(self, path):
            self._c = sqlite3.connect(path)

        def __await__(self):
            async def ready():
                return self

            return ready().__await__()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self._c.close()

        @property
        def row_factory(self):
            return self._c.row_factory

        @row_factory.setter
        def row_factory(self, v):
            self._c.row_factory = v

        async def execute(self, sql, args=()):
            return Cursor(self._c.execute(sql, args))

        async def executescript(self, sql):
            self._c.executescript(sql)

        async def executemany(self, sql, seq):
            self._c.executemany(sql, seq)

        async def commit(self):
            self._c.commit()

        async def rollback(self):
            self._c.rollback()

        async def close(self):
            self._c.close()

    mod.connect = lambda path: Conn(path)
    sys.modules["aiosqlite"] = mod
