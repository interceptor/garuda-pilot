"""SQLite database management for garuda-pilot."""

from __future__ import annotations

import aiosqlite
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    completed_at   TEXT,
    source         TEXT DEFAULT 'log',
    log_line_start INTEGER,
    log_line_end   INTEGER,
    UNIQUE(started_at, log_line_start)
);

CREATE TABLE IF NOT EXISTS package_operations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    action         TEXT NOT NULL,
    package_name   TEXT NOT NULL,
    old_version    TEXT,
    new_version    TEXT,
    description    TEXT,
    category       TEXT,
    is_trivial     INTEGER DEFAULT 0,
    is_patch       INTEGER DEFAULT 0,
    risk_flags     TEXT
);
CREATE INDEX IF NOT EXISTS idx_pkgops_txn ON package_operations(transaction_id);
CREATE INDEX IF NOT EXISTS idx_pkgops_pkg ON package_operations(package_name);
CREATE INDEX IF NOT EXISTS idx_pkgops_action ON package_operations(action);

CREATE TABLE IF NOT EXISTS pending_updates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    package_name TEXT NOT NULL UNIQUE,
    old_version  TEXT NOT NULL,
    new_version  TEXT NOT NULL,
    description  TEXT,
    url          TEXT,
    old_date     TEXT,
    new_date     TEXT,
    category     TEXT,
    is_trivial   INTEGER DEFAULT 0,
    is_patch     INTEGER DEFAULT 0,
    in_news      INTEGER DEFAULT 0,
    risk_score   INTEGER DEFAULT 0,
    risk_flags   TEXT,
    checked_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    guid               TEXT NOT NULL UNIQUE,
    title              TEXT NOT NULL,
    link               TEXT NOT NULL,
    published_at       TEXT NOT NULL,
    mentioned_packages TEXT,
    description        TEXT DEFAULT '',
    is_read            INTEGER DEFAULT 0,
    fetched_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS health_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL,
    source     TEXT DEFAULT 'auto',
    results    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hardware_profile (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    gpu_vendor            TEXT,
    gpu_model             TEXT,
    kernel                TEXT,
    kernel_version        TEXT,
    nvidia_module_loaded  INTEGER DEFAULT 0,
    detected_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS security_advisories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    packages    TEXT NOT NULL,
    status      TEXT NOT NULL,
    severity    TEXT NOT NULL,
    type        TEXT DEFAULT '',
    affected    TEXT DEFAULT '',
    fixed       TEXT DEFAULT '',
    cves        TEXT DEFAULT '',
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS garuda_news (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    guid               TEXT NOT NULL UNIQUE,
    title              TEXT NOT NULL,
    link               TEXT NOT NULL,
    published_at       TEXT NOT NULL,
    mentioned_packages TEXT,
    description        TEXT DEFAULT '',
    is_read            INTEGER DEFAULT 0,
    fetched_at         TEXT NOT NULL
);
"""


class Database:
    """Async SQLite database wrapper."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._init_schema()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def _init_schema(self) -> None:
        await self._db.executescript(SCHEMA_SQL)
        # Set schema version if not present
        async with self._db.execute(
            "SELECT value FROM _meta WHERE key = 'schema_version'"
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                await self._db.execute(
                    "INSERT INTO _meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                current_version = int(row["value"])
                if current_version < SCHEMA_VERSION:
                    await self._migrate(current_version)
                    await self._db.execute(
                        "UPDATE _meta SET value = ? WHERE key = 'schema_version'",
                        (str(SCHEMA_VERSION),),
                    )
        await self._db.commit()

    async def _migrate(self, from_version: int) -> None:
        """Run migrations from from_version to SCHEMA_VERSION."""
        if from_version < 2:
            # v2: add description and is_read columns to news table
            for col_sql in (
                "ALTER TABLE news ADD COLUMN description TEXT DEFAULT ''",
                "ALTER TABLE news ADD COLUMN is_read INTEGER DEFAULT 0",
            ):
                try:
                    await self._db.execute(col_sql)
                except Exception:
                    pass  # Column may already exist

        if from_version < 3:
            # v3: security_advisories + garuda_news tables (created by SCHEMA_SQL)
            # Add new columns to pending_updates
            for col_sql in (
                "ALTER TABLE pending_updates ADD COLUMN security_severity TEXT DEFAULT ''",
                "ALTER TABLE pending_updates ADD COLUMN is_flagged INTEGER DEFAULT 0",
            ):
                try:
                    await self._db.execute(col_sql)
                except Exception:
                    pass  # Column may already exist

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database not connected"
        return self._db

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        return await self._db.execute(sql, params)

    async def executemany(self, sql: str, params_seq) -> aiosqlite.Cursor:
        return await self._db.executemany(sql, params_seq)

    async def fetchone(self, sql: str, params: tuple = ()):
        async with self._db.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()):
        async with self._db.execute(sql, params) as cursor:
            return await cursor.fetchall()

    async def commit(self) -> None:
        await self._db.commit()

    async def get_max_log_line(self) -> int:
        """Get the highest log_line_end from imported transactions."""
        row = await self.fetchone(
            "SELECT COALESCE(MAX(log_line_end), 0) as max_line FROM transactions"
        )
        return row["max_line"] if row else 0
