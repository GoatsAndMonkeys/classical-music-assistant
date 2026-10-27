"""SQLite store: WAL mode, numbered migrations, one writer connection (PLAN.md §4, §5.6)."""

from __future__ import annotations

import asyncio
import functools
import sqlite3
import threading
import time
import zlib
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from importlib import resources
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

SCHEMA_VERSION = 1


def _migrations() -> list[tuple[int, str]]:
    out = []
    for f in resources.files("classical_core.migrations").iterdir():
        name = f.name
        if name.endswith(".sql") and name[:4].isdigit():
            out.append((int(name[:4]), f.read_text(encoding="utf-8")))
    return sorted(out)


def connect(path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    p = Path(path)
    if readonly:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False, isolation_level=None)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(p, check_same_thread=False, isolation_level=None)
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")
    con.row_factory = sqlite3.Row
    return con


def migrate(con: sqlite3.Connection) -> int:
    """Apply pending numbered migrations, each in one transaction. Returns the schema version."""
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    current = int(row[0]) if row else 0
    for num, sql in _migrations():
        if num <= current:
            continue
        con.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _split_sql(sql):
                con.execute(stmt)
            con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(num),))
            con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('generation', '0')")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        current = num
    return current


def _split_sql(sql: str) -> list[str]:
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


class Database:
    """The background contract (PLAN.md §5.6): no SQLite call runs on MA's event loop.

    One writer thread owns the read-write connection, so every write is serialised through it; a small
    pool of reader threads each hold a read-only connection (WAL readers see the last committed state and
    never block the writer). `write(fn, *args)` and `read(fn, con, *args)` run `fn` there and await it."""

    def __init__(self, path: str | Path, readers: int = 2) -> None:
        self.path = Path(path)
        self.con: sqlite3.Connection | None = None
        self._wx = ThreadPoolExecutor(1, thread_name_prefix="classical-db-writer")
        self._rx = ThreadPoolExecutor(readers, thread_name_prefix="classical-db-reader")
        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()

    async def open(self) -> int:
        def _open() -> int:
            self.con = connect(self.path)
            return migrate(self.con)

        return await self.write(_open)

    async def write(self, fn: Callable[..., T], *args: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._wx, functools.partial(fn, *args))

    async def read(self, fn: Callable[..., T], *args: Any) -> T:
        """`fn(con, *args)` on a reader thread with that thread's read-only connection."""
        def job() -> T:
            con = getattr(self._local, "con", None)
            if con is None:
                con = self._local.con = connect(self.path, readonly=True)
                with self._readers_lock:
                    self._readers.append(con)
            return fn(con, *args)

        return await asyncio.get_running_loop().run_in_executor(self._rx, job)

    async def close(self) -> None:
        """Let running statements finish (an album's transaction commits or rolls back), then close."""
        def _close() -> None:
            self._rx.shutdown(wait=True)
            for con in self._readers:
                con.close()
            self._wx.shutdown(wait=True)
            if self.con is not None:
                self.con.close()

        await asyncio.to_thread(_close)


class SqliteMBCache:
    """`mb_cache` table as the MB client's cache (good 90 days, errors 1 day). `run` executes the SQL off the
    event loop (the writer thread); without it the calls run inline (tests)."""

    def __init__(self, con: sqlite3.Connection,
                 run: Callable[..., Awaitable[Any]] | None = None) -> None:
        self.con = con
        self._run = run

    async def _call(self, fn: Callable[..., T], *args: Any) -> T:
        return fn(*args) if self._run is None else await self._run(fn, *args)

    def _get(self, url: str) -> tuple[int, bytes] | None:
        row = self.con.execute("SELECT status, body_zlib, expires_at FROM mb_cache WHERE url=?", (url,)).fetchone()
        if row is None or row["expires_at"] < time.time():
            return None
        return row["status"], zlib.decompress(row["body_zlib"]) if row["body_zlib"] else b""

    def _put(self, url: str, status: int, body: bytes, ttl: int) -> None:
        now = time.time()
        self.con.execute("INSERT OR REPLACE INTO mb_cache(url, status, body_zlib, fetched_at, expires_at) "
                         "VALUES (?,?,?,?,?)", (url, status, zlib.compress(body) if body else None, now, now + ttl))

    async def get(self, url: str) -> tuple[int, bytes] | None:
        return await self._call(self._get, url)

    async def put(self, url: str, status: int, body: bytes, ttl: int) -> None:
        await self._call(self._put, url, status, body, ttl)
