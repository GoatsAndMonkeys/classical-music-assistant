"""SQLite store: WAL mode, numbered migrations, one writer connection (PLAN.md §4, §5.6)."""

from __future__ import annotations

import sqlite3
import time
import zlib
from importlib import resources
from pathlib import Path

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


class SqliteMBCache:
    """`mb_cache` table as the MB client's cache (good 90 days, errors 1 day)."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con

    async def get(self, url: str) -> tuple[int, bytes] | None:
        row = self.con.execute("SELECT status, body_zlib, expires_at FROM mb_cache WHERE url=?", (url,)).fetchone()
        if row is None or row["expires_at"] < time.time():
            return None
        return row["status"], zlib.decompress(row["body_zlib"]) if row["body_zlib"] else b""

    async def put(self, url: str, status: int, body: bytes, ttl: int) -> None:
        now = time.time()
        self.con.execute("INSERT OR REPLACE INTO mb_cache(url, status, body_zlib, fetched_at, expires_at) "
                         "VALUES (?,?,?,?,?)", (url, status, zlib.compress(body) if body else None, now, now + ttl))
