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
from .analysis import hardware, flatpak_parser, pipx_parser

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

    # Import flatpak and pipx history
    await _import_flatpak_history(db)
    await _import_pipx_packages(db)

    # Backfill or refresh is_explicit flags
    await _refresh_explicit_flags(db)

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


async def _refresh_explicit_flags(db: Database) -> None:
    """Set is_explicit on package_operations based on current pacman -Qe.

    Runs once after schema migration, then on every startup to catch changes.
    For packages no longer installed, the flag stays as-is (best effort).
    """
    import asyncio
    row = await db.fetchone("SELECT value FROM _meta WHERE key = 'needs_explicit_backfill'")
    needs_backfill = row and row["value"] == "1"

    try:
        proc = await asyncio.create_subprocess_exec(
            "pacman", "-Qe",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        explicit = {line.split()[0] for line in stdout.decode().splitlines() if line.strip()}
    except Exception:
        return

    if not explicit:
        return

    placeholders = ",".join("?" * len(explicit))
    await db.execute(
        f"UPDATE package_operations SET is_explicit = 1 WHERE package_name IN ({placeholders})",
        tuple(explicit),
    )
    await db.execute(
        f"UPDATE package_operations SET is_explicit = 0 WHERE package_name NOT IN ({placeholders}) AND is_explicit = 1",
        tuple(explicit),
    )

    if needs_backfill:
        await db.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('needs_explicit_backfill', '0')"
        )
    await db.commit()
    print(f"Refreshed is_explicit flags for {len(explicit)} packages")


async def _import_flatpak_history(db: Database) -> None:
    """Import new flatpak install/remove events as transactions."""
    row = await db.fetchone("SELECT value FROM _meta WHERE key = 'flatpak_cursor'")
    cursor = row["value"] if row else None

    transactions = await flatpak_parser.parse_flatpak_history(last_cursor=cursor)
    if not transactions:
        return

    app_names = await flatpak_parser.get_flatpak_apps()
    new_cursor = cursor

    for txn in transactions:
        result = await db.execute(
            "INSERT INTO transactions (started_at, completed_at, source) VALUES (?, ?, 'flatpak')",
            (txn.started_at, txn.started_at),
        )
        txn_id = result.lastrowid
        for op in txn.operations:
            name = app_names.get(op.app_id, op.app_id)
            await db.execute(
                """INSERT INTO package_operations
                   (transaction_id, action, package_name, new_version, category)
                   VALUES (?, ?, ?, ?, 'flatpak')""",
                (txn_id, op.action, op.app_id, op.version),
            )
            await db.execute(
                "INSERT INTO transaction_logs (transaction_id, log_type, message) VALUES (?, 'command', ?)",
                (txn_id, f"flatpak install {op.app_id}"),
            )
        if new_cursor is None or txn.started_at > new_cursor:
            new_cursor = txn.started_at

    await db.execute(
        "INSERT OR REPLACE INTO _meta (key, value) VALUES ('flatpak_cursor', ?)",
        (new_cursor,),
    )
    await db.commit()
    print(f"Imported {len(transactions)} flatpak events")


async def _import_pipx_packages(db: Database) -> None:
    """Snapshot pipx packages, inserting any newly seen ones as transactions."""
    rows = await db.fetchall(
        "SELECT po.package_name FROM package_operations po "
        "JOIN transactions t ON t.id = po.transaction_id "
        "WHERE t.source = 'pipx'"
    )
    known = {row["package_name"] for row in rows}

    snapshot = await pipx_parser.build_pipx_snapshot(known)
    if not snapshot:
        return

    result = await db.execute(
        "INSERT INTO transactions (started_at, completed_at, source) VALUES (?, ?, 'pipx')",
        (snapshot.started_at, snapshot.started_at),
    )
    txn_id = result.lastrowid
    for op in snapshot.operations:
        await db.execute(
            """INSERT INTO package_operations
               (transaction_id, action, package_name, new_version, category)
               VALUES (?, ?, ?, ?, 'pipx')""",
            (txn_id, op.action, op.package_name, op.version),
        )
        await db.execute(
            "INSERT INTO transaction_logs (transaction_id, log_type, message) VALUES (?, 'command', ?)",
            (txn_id, f"pipx install {op.package_name}"),
        )

    await db.commit()
    print(f"Imported {len(snapshot.operations)} pipx packages")


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
    from .routes import dashboard, history, preview, news, health, security, changelog, about, snapshots, pacnew, settings, packages
    app.include_router(dashboard.router)
    app.include_router(history.router)
    app.include_router(preview.router)
    app.include_router(news.router)
    app.include_router(health.router)
    app.include_router(security.router)
    app.include_router(snapshots.router)
    app.include_router(pacnew.router)
    app.include_router(packages.router)
    app.include_router(settings.router)
    app.include_router(changelog.router)
    app.include_router(about.router)

    return app
