"""SQLite database management for garuda-pilot."""

from __future__ import annotations

import aiosqlite
from pathlib import Path

SCHEMA_VERSION = 7

# Only _meta is created outside migrations. Everything else is in versioned
# migration blocks so fresh installs and upgrades follow the identical path
# and always end up with exactly the same schema.
_META_SQL = """
CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
        # Bootstrap only _meta, then migrate from whatever version the DB is at.
        # Fresh installs start at version 0 and run all migrations — same path
        # as upgrades, so the schema is always the result of migrations in order.
        await self._db.executescript(_META_SQL)

        async with self._db.execute(
            "SELECT value FROM _meta WHERE key = 'schema_version'"
        ) as cursor:
            row = await cursor.fetchone()

        current_version = int(row["value"]) if row else 0

        if current_version < SCHEMA_VERSION:
            await self._migrate(current_version)
            await self._db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        await self._db.commit()

    async def _migrate(self, from_version: int) -> None:
        """Run all pending migrations in order from from_version to SCHEMA_VERSION.

        Rules:
          - New tables: CREATE TABLE IF NOT EXISTS (idempotent).
          - New columns: ALTER TABLE wrapped in try/except (SQLite has no IF NOT EXISTS).
          - Never edit an existing migration block. Add a new block and bump SCHEMA_VERSION.
        """
        if from_version < 1:
            # v1: initial schema
            await self._db.executescript("""
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
            """)

        if from_version < 2:
            # v2: description and is_read on news
            for sql in (
                "ALTER TABLE news ADD COLUMN description TEXT DEFAULT ''",
                "ALTER TABLE news ADD COLUMN is_read INTEGER DEFAULT 0",
            ):
                try:
                    await self._db.execute(sql)
                except Exception:
                    pass

        if from_version < 3:
            # v3: security_advisories + garuda_news tables;
            #     security_severity + is_flagged on pending_updates
            await self._db.executescript("""
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
            """)
            for sql in (
                "ALTER TABLE pending_updates ADD COLUMN security_severity TEXT DEFAULT ''",
                "ALTER TABLE pending_updates ADD COLUMN is_flagged INTEGER DEFAULT 0",
            ):
                try:
                    await self._db.execute(sql)
                except Exception:
                    pass

        if from_version < 4:
            # v4: transaction_logs; backfill flag for existing transactions
            await self._db.executescript("""
                CREATE TABLE IF NOT EXISTS transaction_logs (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
                    log_type       TEXT NOT NULL,
                    message        TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_txnlogs_txn ON transaction_logs(transaction_id);
            """)
            await self._db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES ('needs_log_backfill', '1')"
            )

        if from_version < 6:
            # v6: relax UNIQUE constraint on transactions to allow flatpak/pipx
            # entries that have no log_line_start. Recreate using PRAGMA to
            # defer FK checks during table swap.
            await self._db.execute("PRAGMA foreign_keys=OFF")
            await self._db.executescript("""
                CREATE TABLE IF NOT EXISTS transactions_new (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at     TEXT NOT NULL,
                    completed_at   TEXT,
                    source         TEXT DEFAULT 'log',
                    log_line_start INTEGER,
                    log_line_end   INTEGER
                );
                INSERT OR IGNORE INTO transactions_new
                    SELECT * FROM transactions;
                DROP TABLE transactions;
                ALTER TABLE transactions_new RENAME TO transactions;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_txn_log_unique
                    ON transactions(started_at, log_line_start)
                    WHERE log_line_start IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_txn_source ON transactions(source);
            """)
            await self._db.execute("PRAGMA foreign_keys=ON")
            await self._db.execute("PRAGMA foreign_key_check")

        if from_version < 7:
            # v7: add is_explicit flag to package_operations
            try:
                await self._db.execute(
                    "ALTER TABLE package_operations ADD COLUMN is_explicit INTEGER DEFAULT 0"
                )
            except Exception:
                pass
            # Schedule backfill of is_explicit from current pacman -Qe
            await self._db.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES ('needs_explicit_backfill', '1')"
            )

        if from_version < 5:
            # v5: fix for databases created fresh at v4, which bypassed the v3
            # migration and never received security_severity + is_flagged.
            # No-ops on databases that already have these columns.
            for sql in (
                "ALTER TABLE pending_updates ADD COLUMN security_severity TEXT DEFAULT ''",
                "ALTER TABLE pending_updates ADD COLUMN is_flagged INTEGER DEFAULT 0",
            ):
                try:
                    await self._db.execute(sql)
                except Exception:
                    pass

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
