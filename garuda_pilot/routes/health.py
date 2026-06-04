"""Health routes — system health check display."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..analysis import health as health_mod

router = APIRouter()


@router.get("/health")
async def health_page(request: Request):
    db = request.app.state.db
    templates = request.app.state.templates

    # Load latest snapshot (if any)
    latest, checked_at = await health_mod.load_latest_snapshot(db)

    # If no snapshot exists, run a check now
    if latest is None:
        latest = await health_mod.run_health_check()
        await health_mod.store_snapshot(db, latest, source="auto")
        row = await db.fetchone(
            "SELECT checked_at FROM health_snapshots ORDER BY id DESC LIMIT 1"
        )
        checked_at = row["checked_at"] if row else None

    # Load history and comparison
    history = await health_mod.load_snapshot_history(db)
    comparison = await health_mod.compare_with_previous(db, latest)

    return templates.TemplateResponse(request, "health.html", {
        "request": request,
        "active_page": "health",
        "result": latest,
        "checked_at": checked_at,
        "history": history,
        "comparison": comparison,
        "check_names": health_mod.CHECK_NAMES,
    })


@router.post("/htmx/health-refresh")
async def health_refresh(request: Request):
    """HTMX: run a fresh health check and return updated content."""
    db = request.app.state.db
    templates = request.app.state.templates

    result = await health_mod.run_health_check()
    await health_mod.store_snapshot(db, result, source="manual")

    row = await db.fetchone(
        "SELECT checked_at FROM health_snapshots ORDER BY id DESC LIMIT 1"
    )
    checked_at = row["checked_at"] if row else None
    history = await health_mod.load_snapshot_history(db)
    comparison = await health_mod.compare_with_previous(db, result)

    return templates.TemplateResponse(request, "health_content.html", {
        "request": request,
        "result": result,
        "checked_at": checked_at,
        "history": history,
        "comparison": comparison,
        "check_names": health_mod.CHECK_NAMES,
    })
