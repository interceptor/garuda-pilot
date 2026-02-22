"""Dashboard route."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..analysis import health as health_mod
from .about import _list_backups

router = APIRouter()


@router.get("/")
async def dashboard(request: Request):
    db = request.app.state.db
    templates = request.app.state.templates

    # Transaction count
    row = await db.fetchone("SELECT COUNT(*) as cnt FROM transactions")
    transaction_count = row["cnt"]

    # Last transaction
    last_txn = await db.fetchone(
        "SELECT * FROM transactions ORDER BY id DESC LIMIT 1"
    )

    # Pending updates count
    pending = await db.fetchone("SELECT COUNT(*) as cnt FROM pending_updates")
    pending_count = pending["cnt"]

    # News count
    news_row = await db.fetchone("SELECT COUNT(*) as cnt FROM news")
    news_count = news_row["cnt"]

    # Latest health status
    health_result, health_checked = await health_mod.load_latest_snapshot(db)

    # Security advisory counts (https://security.archlinux.org/)
    vuln_row = await db.fetchone(
        "SELECT COUNT(*) as cnt FROM security_advisories WHERE status = 'Vulnerable'"
    )
    vuln_count = vuln_row["cnt"] if vuln_row else 0
    total_sec = await db.fetchone("SELECT COUNT(*) as cnt FROM security_advisories")
    total_sec_count = total_sec["cnt"] if total_sec else 0

    # Backup info
    db_path = request.app.state.config.db_path
    backups = _list_backups(db_path)
    last_backup = backups[0] if backups else None

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "active_page": "dashboard",
        "transaction_count": transaction_count,
        "last_transaction": last_txn,
        "pending_count": pending_count,
        "news_count": news_count,
        "health_result": health_result,
        "health_checked": health_checked,
        "vuln_count": vuln_count,
        "total_sec_count": total_sec_count,
        "last_backup": last_backup,
    })
