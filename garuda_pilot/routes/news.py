"""News routes — Arch Linux + Garuda Linux news feed display."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..analysis import news as news_mod
from ..analysis import garuda_news as garuda_mod

router = APIRouter()


async def _ensure_news(db) -> None:
    """Fetch and store news from both sources if stale (>2h)."""
    # Arch news (https://archlinux.org/feeds/news/)
    row = await db.fetchone(
        "SELECT fetched_at FROM news ORDER BY fetched_at DESC LIMIT 1"
    )
    if row:
        from datetime import datetime, timedelta, timezone
        try:
            fetched = datetime.fromisoformat(row["fetched_at"])
            if datetime.now(timezone.utc) - fetched < timedelta(hours=2):
                row = None  # Mark as fresh, skip fetch
        except (ValueError, TypeError):
            pass
    if row is None:
        # Check if we have any data at all
        count = await db.fetchone("SELECT COUNT(*) as cnt FROM news")
        if count and count["cnt"] == 0:
            row = True  # Force fetch if empty

    if row:
        entries = await news_mod.fetch_news(months=6)
        if entries:
            await news_mod.store_news(db, entries)

    # Garuda news (https://forum.garudalinux.org/c/announcements/16.rss)
    await garuda_mod.ensure_garuda_news(db)


async def _build_news_items(db) -> list[dict]:
    """Build enriched news items list from both Arch and Garuda sources."""
    # Arch news
    arch_rows = await db.fetchall(
        "SELECT * FROM news ORDER BY published_at DESC"
    )
    # Garuda news
    garuda_rows = await db.fetchall(
        "SELECT * FROM garuda_news ORDER BY published_at DESC"
    )

    # Collect all mentioned packages across all items
    all_packages: set[str] = set()
    items = []

    for row in arch_rows:
        pkgs = (row["mentioned_packages"] or "").split("|")
        pkgs = [p for p in pkgs if p]
        all_packages.update(pkgs)
        items.append({
            "id": row["id"],
            "source": "arch",
            "title": row["title"],
            "link": row["link"],
            "published_at": row["published_at"],
            "mentioned_packages": pkgs,
            "description": row["description"] or "",
            "is_read": bool(row["is_read"]),
        })

    for row in garuda_rows:
        pkgs = (row["mentioned_packages"] or "").split("|")
        pkgs = [p for p in pkgs if p]
        all_packages.update(pkgs)
        items.append({
            "id": row["id"],
            "source": "garuda",
            "title": row["title"],
            "link": row["link"],
            "published_at": row["published_at"],
            "mentioned_packages": pkgs,
            "description": row["description"] or "",
            "is_read": bool(row["is_read"]),
        })

    # Sort combined list by date descending
    items.sort(key=lambda x: x["published_at"], reverse=True)

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

    return templates.TemplateResponse(request, "news.html", {
        "request": request,
        "active_page": "news",
        "news_items": items,
    })


@router.post("/htmx/news-refresh")
async def news_refresh(request: Request):
    """HTMX: force-refresh news from both RSS sources."""
    db = request.app.state.db
    templates = request.app.state.templates

    # Fetch both sources
    entries = await news_mod.fetch_news(months=6)
    if entries:
        await news_mod.store_news(db, entries)

    garuda_entries = await garuda_mod.fetch_garuda_news(months=6)
    if garuda_entries:
        await garuda_mod.store_garuda_news(db, garuda_entries)

    items = await _build_news_items(db)

    return templates.TemplateResponse(request, "news_content.html", {
        "request": request,
        "news_items": items,
    })


@router.post("/htmx/news-mark-read/{source}/{news_id}")
async def news_mark_read(source: str, news_id: int, request: Request):
    """HTMX: toggle read/unread state for a news item."""
    db = request.app.state.db

    table = "garuda_news" if source == "garuda" else "news"
    row = await db.fetchone(f"SELECT is_read FROM {table} WHERE id = ?", (news_id,))
    if not row:
        return HTMLResponse("", status_code=404)

    new_state = 0 if row["is_read"] else 1
    await db.execute(f"UPDATE {table} SET is_read = ? WHERE id = ?", (new_state, news_id))
    await db.commit()

    if new_state:
        return HTMLResponse(
            f'<button class="btn btn-sm btn-secondary" '
            f'hx-post="/htmx/news-mark-read/{source}/{news_id}" '
            f'hx-swap="outerHTML">Mark Unread</button>'
        )
    else:
        return HTMLResponse(
            f'<button class="btn btn-sm btn-accent" '
            f'hx-post="/htmx/news-mark-read/{source}/{news_id}" '
            f'hx-swap="outerHTML">Mark Read</button>'
        )
