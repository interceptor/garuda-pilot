"""Snapshots route — btrfs snapshot history via snapper."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..analysis import snapshots as snap_mod

router = APIRouter()


@router.get("/snapshots")
async def snapshots_page(request: Request):
    templates = request.app.state.templates
    status = await snap_mod.get_status()
    return templates.TemplateResponse(request, "snapshots.html", {
        "request": request,
        "active_page": "snapshots",
        "status": status,
    })


@router.post("/htmx/snapshot-refresh")
async def snapshot_refresh(request: Request):
    """HTMX: reload the snapshot list."""
    templates = request.app.state.templates
    status = await snap_mod.get_status()
    return templates.TemplateResponse(request, "snapshots_list.html", {
        "request": request,
        "status": status,
    })


@router.post("/htmx/snapshot-create")
async def snapshot_create(request: Request):
    """Create a manual single snapshot (requires snapper permissions)."""
    templates = request.app.state.templates
    status = await snap_mod.get_status()

    if not status.can_create:
        return HTMLResponse(
            '<span style="color: var(--warning);">Cannot create snapshot: insufficient permissions. '
            + (status.permission_hint or "") + "</span>"
        )

    num, err = await snap_mod.create_snapshot(status.config, "manual (garuda-pilot)")
    if err:
        return HTMLResponse(f'<span style="color: var(--warning);">Error: {err}</span>')

    # Refresh the snapshot list
    status = await snap_mod.get_status()
    return templates.TemplateResponse(request, "snapshots_list.html", {
        "request": request,
        "status": status,
        "flash": f"Snapshot #{num} created.",
    })
