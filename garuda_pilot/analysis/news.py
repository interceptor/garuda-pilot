"""Arch Linux news RSS fetcher and parser.

Fetches https://archlinux.org/feeds/news/, extracts items with titles,
links, dates, and mentioned package names from <code> and <li> tags.
"""

from __future__ import annotations

import asyncio
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

ARCH_NEWS_RSS = "https://archlinux.org/feeds/news/"
FETCH_TIMEOUT = 15

# Match package names in HTML <code> tags — RSS may have literal or entity-encoded
_CODE_RE = re.compile(r"(?:<code>|&lt;code&gt;)([^<&]+?)(?:</code>|&lt;/code&gt;)")
# Match package names in <li> tags (some news items list packages this way)
_LI_RE = re.compile(r"(?:<li>|&lt;li&gt;)\s*([a-z0-9][-a-z0-9.]*)\s*(?:</li>|&lt;/li&gt;)", re.IGNORECASE)
# Valid Arch package name pattern (must contain a letter)
_PKG_NAME_RE = re.compile(r"^(?=.*[a-z])[a-z0-9]+(-[a-z0-9]+)*$")
_PKG_NAME_MAX = 60
# Reject hex-like strings (e.g. GPG fingerprints from <code> tags)
_HEX_RE = re.compile(r"^(?:0x)?[0-9a-f]{16,}$")
# Manual intervention patterns in titles
_INTERVENTION_RE = re.compile(r"manual intervention|requires manual|action required", re.IGNORECASE)


@dataclass
class NewsEntry:
    """A single Arch news item."""
    title: str
    link: str
    published_at: datetime
    description: str = ""
    mentioned_packages: list[str] = field(default_factory=list)


async def fetch_news(months: int = 6) -> list[NewsEntry]:
    """Fetch Arch news RSS and return items from the last N months."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(ARCH_NEWS_RSS, timeout=FETCH_TIMEOUT)
            resp.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException):
        return []

    return parse_rss(resp.text, months=months)


def parse_rss(xml_text: str, months: int = 6) -> list[NewsEntry]:
    """Parse Arch news RSS XML into NewsEntry list."""
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

        # Extract package names from <code> tags in description
        packages = _extract_packages(desc_text)

        entries.append(NewsEntry(
            title=title_el.text or "",
            link=link_el.text or "",
            published_at=pub_dt,
            description=desc_text,
            mentioned_packages=packages,
        ))

    return entries


def _extract_packages(html_desc: str) -> list[str]:
    """Extract valid package names from HTML <code> and <li> tags."""
    raw = _CODE_RE.findall(html_desc)
    raw.extend(_LI_RE.findall(html_desc))
    packages = []
    seen: set[str] = set()
    for name in raw:
        name = name.strip().lower()
        if (name not in seen and len(name) <= _PKG_NAME_MAX
                and _PKG_NAME_RE.match(name) and not _HEX_RE.match(name)):
            seen.add(name)
            packages.append(name)
    return packages


def get_all_news_packages(entries: list[NewsEntry]) -> set[str]:
    """Collect all unique package names mentioned across all news entries."""
    pkgs: set[str] = set()
    for entry in entries:
        pkgs.update(entry.mentioned_packages)
    return pkgs


async def store_news(db, entries: list[NewsEntry]) -> None:
    """Store news entries in the database, deduplicating by guid (link)."""
    now = datetime.now(timezone.utc).isoformat()
    for entry in entries:
        pkg_str = "|".join(entry.mentioned_packages)
        await db.execute(
            """INSERT OR REPLACE INTO news
               (guid, title, link, published_at, mentioned_packages,
                description, is_read, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?,
                       COALESCE((SELECT is_read FROM news WHERE guid = ?), 0),
                       ?)""",
            (entry.link, entry.title, entry.link,
             entry.published_at.isoformat(), pkg_str,
             entry.description, entry.link, now),
        )
    await db.commit()


def is_manual_intervention(title: str) -> bool:
    """Check if a news title indicates manual intervention is required."""
    return bool(_INTERVENTION_RE.search(title))


def strip_html_tags(html_text: str) -> str:
    """Convert HTML to plain text for safe display.

    Converts block-level tags to newlines, strips remaining tags,
    and decodes HTML entities.
    """
    if not html_text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html_text, flags=re.IGNORECASE)
    text = re.sub(r"</(?:p|div|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<(?:p|div|ul|ol|li|h[1-6])[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def check_installed_packages(packages: set[str]) -> set[str]:
    """Check which of the given packages are installed on this system.

    Runs a single `pacman -Q` call and intersects with the given set.
    """
    if not packages:
        return set()

    try:
        proc = await asyncio.create_subprocess_exec(
            "pacman", "-Q",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (FileNotFoundError, asyncio.TimeoutError):
        return set()

    installed = set()
    for line in stdout.decode(errors="replace").splitlines():
        parts = line.split()
        if parts:
            installed.add(parts[0])

    return packages & installed
