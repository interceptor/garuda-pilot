"""News routes — Arch Linux news feed display."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..analysis import news as news_mod

router = APIRouter()


async def _ensure_news(db) -> None:
    """Fetch and store news if we have none or data is stale (>2h)."""
    row = await db.fetchone(
        """SELECT fetched_at FROM news
           ORDER BY fetched_at DESC LIMIT 1"""
    )
    if row:
        from datetime import datetime, timedelta, timezone
        try:
            fetched = datetime.fromisoformat(row["fetched_at"])
            if datetime.now(timezone.utc) - fetched < timedelta(hours=2):
                return  # Fresh enough
        except (ValueError, TypeError):
            pass

    entries = await news_mod.fetch_news(months=6)
    if entries:
        await news_mod.store_news(db, entries)


async def _build_news_items(db) -> list[dict]:
    """Build enriched news items list from DB rows."""
    rows = await db.fetchall(
        "SELECT * FROM news ORDER BY published_at DESC"
    )

    # Collect all mentioned packages across all items
    all_packages: set[str] = set()
    items = []
    for row in rows:
        pkgs = (row["mentioned_packages"] or "").split("|")
        pkgs = [p for p in pkgs if p]
        all_packages.update(pkgs)
        items.append({
            "id": row["id"],
            "title": row["title"],
            "link": row["link"],
            "published_at": row["published_at"],
            "mentioned_packages": pkgs,
            "description": row["description"] or "",
            "is_read": bool(row["is_read"]),
        })

    # Bulk check which mentioned packages are installed
    installed = await news_mod.check_installed_packages(all_packages)

    # Enrich items with relevance info
    for item in items:
        item_pkgs = set(item["mentioned_packages"])
        item["installed_packages"] = item_pkgs & installed
        item["affects_system"] = bool(item_pkgs & installed)
        item["manual_intervention"] = news_mod.is_manual_intervention(item["title"])
        item["description_text"] = news_mod.strip_html_tags(item["description"])

    return items


@router.get("/news")
async def news_page(request: Request):
    db = request.app.state.db
    templates = request.app.state.templates

    await _ensure_news(db)
    items = await _build_news_items(db)

    return templates.TemplateResponse("news.html", {
        "request": request,
        "active_page": "news",
        "news_items": items,
    })


@router.post("/htmx/news-refresh")
async def news_refresh(request: Request):
    """HTMX: force-refresh news from RSS."""
    db = request.app.state.db
    templates = request.app.state.templates

    entries = await news_mod.fetch_news(months=6)
    if entries:
        await news_mod.store_news(db, entries)

    items = await _build_news_items(db)

    return templates.TemplateResponse("news_content.html", {
        "request": request,
        "news_items": items,
    })


@router.post("/htmx/news-mark-read/{news_id}")
async def news_mark_read(news_id: int, request: Request):
    """HTMX: toggle read/unread state for a news item."""
    db = request.app.state.db

    # Get current state
    row = await db.fetchone("SELECT is_read FROM news WHERE id = ?", (news_id,))
    if not row:
        return HTMLResponse("", status_code=404)

    new_state = 0 if row["is_read"] else 1
    await db.execute("UPDATE news SET is_read = ? WHERE id = ?", (new_state, news_id))
    await db.commit()

    if new_state:
        return HTMLResponse(
            f'<button class="btn btn-sm btn-secondary" '
            f'hx-post="/htmx/news-mark-read/{news_id}" '
            f'hx-swap="outerHTML">Mark Unread</button>'
        )
    else:
        return HTMLResponse(
            f'<button class="btn btn-sm btn-accent" '
            f'hx-post="/htmx/news-mark-read/{news_id}" '
            f'hx-swap="outerHTML">Mark Read</button>'
        )
