"""Security routes — Arch Security Tracker advisory display."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..analysis import security as sec_mod

router = APIRouter()


@router.get("/security")
async def security_page(request: Request, q: str = ""):
    db = request.app.state.db
    templates = request.app.state.templates

    await sec_mod.ensure_advisories(db)
    items = await sec_mod.get_all_advisories(db)

    # Stats — count only Vulnerable since that's the default filter
    vuln_count = sum(1 for i in items if i["status"] == "Vulnerable")
    fixed_count = sum(1 for i in items if i["status"] == "Fixed")
    sev_counts = {}
    for sev in ("Critical", "High", "Medium", "Low"):
        sev_counts[sev] = sum(
            1 for i in items
            if i["severity"] == sev and i["status"] == "Vulnerable"
        )

    return templates.TemplateResponse(request, "security.html", {
        "request": request,
        "active_page": "security",
        "advisories": items,
        "vuln_count": vuln_count,
        "fixed_count": fixed_count,
        "sev_counts": sev_counts,
        "total": len(items),
        "search_query": q,
    })


@router.post("/htmx/security-refresh")
async def security_refresh(request: Request):
    """HTMX: force-refresh advisories from security tracker."""
    db = request.app.state.db
    templates = request.app.state.templates

    advisories = await sec_mod.fetch_advisories()
    if advisories:
        await sec_mod.store_advisories(db, advisories)

    items = await sec_mod.get_all_advisories(db)

    vuln_count = sum(1 for i in items if i["status"] == "Vulnerable")
    sev_counts = {}
    for sev in ("Critical", "High", "Medium", "Low"):
        sev_counts[sev] = sum(
            1 for i in items
            if i["severity"] == sev and i["status"] == "Vulnerable"
        )

    return templates.TemplateResponse(request, "security_content.html", {
        "request": request,
        "advisories": items,
        "vuln_count": vuln_count,
        "sev_counts": sev_counts,
        "total": len(items),
    })
