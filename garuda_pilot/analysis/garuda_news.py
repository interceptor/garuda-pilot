"""Garuda Linux Forum announcements RSS fetcher.

Fetches https://forum.garudalinux.org/c/announcements/16.rss
Same pattern as the Arch news module — fetch, parse, store, stale-check.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

from .news import NewsEntry, _extract_packages, strip_html_tags

GARUDA_RSS = "https://forum.garudalinux.org/c/announcements/16.rss"
FETCH_TIMEOUT = 15


async def fetch_garuda_news(months: int = 6) -> list[NewsEntry]:
    """Fetch Garuda forum announcements RSS."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(GARUDA_RSS, timeout=FETCH_TIMEOUT)
            resp.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException):
        return []

    return _parse_garuda_rss(resp.text, months=months)


def _parse_garuda_rss(xml_text: str, months: int = 6) -> list[NewsEntry]:
    """Parse Garuda forum RSS into NewsEntry list."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=months * 30)
    entries: list[NewsEntry] = []

    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        pubdate_el = item.find("pubDate")
        desc_el = item.find("description")

        if title_el is None or link_el is None or pubdate_el is None:
            continue

        try:
            pub_dt = parsedate_to_datetime(pubdate_el.text)
        except (ValueError, TypeError):
            continue

        if pub_dt < cutoff:
            continue

        desc_text = desc_el.text if desc_el is not None and desc_el.text else ""
        packages = _extract_packages(desc_text)

        entries.append(NewsEntry(
            title=title_el.text or "",
            link=link_el.text or "",
            published_at=pub_dt,
            description=desc_text,
            mentioned_packages=packages,
        ))

    return entries


async def store_garuda_news(db, entries: list[NewsEntry]) -> None:
    """Store Garuda news in the garuda_news table, deduplicating by guid."""
    now = datetime.now(timezone.utc).isoformat()
    for entry in entries:
        pkg_str = "|".join(entry.mentioned_packages)
        await db.execute(
            """INSERT OR REPLACE INTO garuda_news
               (guid, title, link, published_at, mentioned_packages,
                description, is_read, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?,
                       COALESCE((SELECT is_read FROM garuda_news WHERE guid = ?), 0),
                       ?)""",
            (entry.link, entry.title, entry.link,
             entry.published_at.isoformat(), pkg_str,
             entry.description, entry.link, now),
        )
    await db.commit()


async def ensure_garuda_news(db) -> None:
    """Fetch and store Garuda news if stale (>2h)."""
    row = await db.fetchone(
        "SELECT fetched_at FROM garuda_news ORDER BY fetched_at DESC LIMIT 1"
    )
    if row:
        try:
            fetched = datetime.fromisoformat(row["fetched_at"])
            if datetime.now(timezone.utc) - fetched < timedelta(hours=2):
                return
        except (ValueError, TypeError):
            pass

    entries = await fetch_garuda_news(months=6)
    if entries:
        await store_garuda_news(db, entries)


def get_all_garuda_news_packages(entries: list[NewsEntry]) -> set[str]:
    """Collect all unique package names mentioned across Garuda news."""
    pkgs: set[str] = set()
    for entry in entries:
        pkgs.update(entry.mentioned_packages)
    return pkgs
