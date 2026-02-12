"""Dashboard route."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..analysis import health as health_mod

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

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "active_page": "dashboard",
        "transaction_count": transaction_count,
        "last_transaction": last_txn,
        "pending_count": pending_count,
        "news_count": news_count,
        "health_result": health_result,
        "health_checked": health_checked,
    })
