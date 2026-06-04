"""FastAPI application factory for garuda-pilot."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .config import Config
from .db import Database
from .pacman import log_parser, categorizer
from .analysis import hardware

PKG_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    config: Config = app.state.config
    config.ensure_dirs()

    # Connect database
    db = Database(config.db_path)
    await db.connect()
    app.state.db = db

    # Import new transactions from pacman.log (incremental)
    if config.pacman_log.exists():
        await _backfill_log_events(db, config)
        await _import_pacman_log(db, config)

    # Detect hardware on startup
    hw = await hardware.detect()
    await hardware.store_profile(db, hw)
    print(f"Hardware: GPU={hw.gpu_vendor or 'unknown'} kernel={hw.kernel} nvidia_loaded={hw.nvidia_module_loaded}")

    yield

    # Shutdown
    await db.close()


async def _store_log_events(db: Database, txn_id: int, txn: log_parser.ParsedTransaction) -> None:
    """Store warnings, scriptlet output, and pacman command for a transaction."""
    if txn.pacman_command:
        await db.execute(
            "INSERT INTO transaction_logs (transaction_id, log_type, message) VALUES (?, 'command', ?)",
            (txn_id, txn.pacman_command),
        )
    for msg in txn.warnings:
        await db.execute(
            "INSERT INTO transaction_logs (transaction_id, log_type, message) VALUES (?, 'warning', ?)",
            (txn_id, msg),
        )
    for msg in txn.scriptlet_output:
        await db.execute(
            "INSERT INTO transaction_logs (transaction_id, log_type, message) VALUES (?, 'scriptlet', ?)",
            (txn_id, msg),
        )


async def _import_pacman_log(db: Database, config: Config) -> int:
    """Import new transactions from pacman.log (incremental).

    Returns the number of newly imported transactions.
    """
    from_line = await db.get_max_log_line()
    transactions = log_parser.parse_log(config.pacman_log, from_line=from_line)
    if not transactions:
        return 0

    for txn in transactions:
        cursor = await db.execute(
            """INSERT OR IGNORE INTO transactions
               (started_at, completed_at, source, log_line_start, log_line_end)
               VALUES (?, ?, 'log', ?, ?)""",
            (txn.started_at, txn.completed_at, txn.log_line_start, txn.log_line_end),
        )
        txn_id = cursor.lastrowid
        if txn_id == 0:
            # Already imported (UNIQUE constraint)
            continue

        for op in txn.operations:
            cats = categorizer.categories_str(op.package_name)
            trivial = categorizer.is_trivial(op.package_name)
            patch = False
            if op.old_version and op.new_version:
                patch = categorizer.is_patch_update(op.old_version, op.new_version)

            await db.execute(
                """INSERT INTO package_operations
                   (transaction_id, action, package_name, old_version, new_version,
                    category, is_trivial, is_patch)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (txn_id, op.action, op.package_name, op.old_version, op.new_version,
                 cats, int(trivial), int(patch)),
            )

        await _store_log_events(db, txn_id, txn)

    await db.commit()
    print(f"Imported {len(transactions)} new transactions from pacman.log")
    return len(transactions)


async def _backfill_log_events(db: Database, config: Config) -> None:
    """One-time backfill: re-parse log to add log events to existing transactions."""
    row = await db.fetchone("SELECT value FROM _meta WHERE key = 'needs_log_backfill'")
    if not row or row["value"] != "1":
        return

    print("Backfilling transaction log events from pacman.log...")
    all_txns = log_parser.parse_log(config.pacman_log, from_line=0)

    for txn in all_txns:
        # Find the matching existing transaction
        db_row = await db.fetchone(
            "SELECT id FROM transactions WHERE started_at = ? AND log_line_start = ?",
            (txn.started_at, txn.log_line_start),
        )
        if not db_row:
            continue

        txn_id = db_row["id"]

        # Skip if already has log entries
        existing = await db.fetchone(
            "SELECT COUNT(*) as cnt FROM transaction_logs WHERE transaction_id = ?",
            (txn_id,),
        )
        if existing["cnt"] > 0:
            continue

        await _store_log_events(db, txn_id, txn)

    await db.execute(
        "INSERT OR REPLACE INTO _meta (key, value) VALUES ('needs_log_backfill', '0')"
    )
    await db.commit()
    print("Backfill complete.")


def create_app(config: Config | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if config is None:
        config = Config.load()

    app = FastAPI(
        title="garuda-pilot",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.config = config

    # Static files
    app.mount("/static", StaticFiles(directory=PKG_DIR / "static"), name="static")

    # Templates
    templates = Jinja2Templates(directory=PKG_DIR / "templates")
    app.state.templates = templates

    # Register routes
    from .routes import dashboard, history, preview, news, health, security, changelog, about, snapshots
    app.include_router(dashboard.router)
    app.include_router(history.router)
    app.include_router(preview.router)
    app.include_router(news.router)
    app.include_router(health.router)
    app.include_router(security.router)
    app.include_router(snapshots.router)
    app.include_router(changelog.router)
    app.include_router(about.router)

    return app
