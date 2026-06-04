"""Preview routes — pending upgrade list with search/filters."""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..pacman import checkupdates, query, categorizer
from ..analysis import news as news_mod, hardware, risk as risk_mod
from ..analysis import security as sec_mod
from ..analysis import garuda_news as garuda_mod
from ..analysis import pkg_api
from ..app import _import_pacman_log
from .about import create_backup

router = APIRouter()

# SQL for consistent ordering: risk_score DESC, then by category priority
_ORDER_SQL = """
    ORDER BY risk_score DESC, package_name
"""


async def _refresh_pending(db, /) -> int:
    """Run checkupdates, enrich with pacman info + news + risk, store in DB.

    Returns the number of pending updates found.
    """
    packages = await checkupdates.check_updates()
    if not packages:
        await db.execute("DELETE FROM pending_updates")
        await db.commit()
        return 0

    # Bulk query package info (2 pacman calls total)
    names = [p.name for p in packages]
    info = await query.bulk_query(names)

    # Fetch news, security, garuda news in parallel
    async def _fetch_news():
        entries = await news_mod.fetch_news(months=6)
        if entries:
            await news_mod.store_news(db, entries)
        return entries

    async def _fetch_security():
        await sec_mod.ensure_advisories(db)
        return await sec_mod.get_vulnerable_packages(db)

    async def _fetch_garuda():
        await garuda_mod.ensure_garuda_news(db)
        rows = await db.fetchall(
            "SELECT mentioned_packages FROM garuda_news"
        )
        pkgs: set[str] = set()
        for row in rows:
            for p in (row["mentioned_packages"] or "").split("|"):
                if p:
                    pkgs.add(p)
        return pkgs

    entries, vuln_map, garuda_pkgs = await asyncio.gather(
        _fetch_news(), _fetch_security(), _fetch_garuda()
    )
    news_pkgs = news_mod.get_all_news_packages(entries)

    # Fetch package metadata from Arch API
    # Only fetch for high-priority packages to respect rate limits
    priority_names = [n for n in names if categorizer.categories_str(n) or n in vuln_map]
    if len(priority_names) > 50:
        priority_names = priority_names[:50]
    pkg_metas = await pkg_api.fetch_package_meta(priority_names)
    flagged_set = pkg_api.get_flagged_packages(pkg_metas)
    pending_set = set(names)
    dep_counts = pkg_api.compute_dep_counts(pkg_metas, pending_set)

    # Load hardware profile for risk scoring
    hw = await hardware.load_profile(db)

    now = datetime.now(timezone.utc).isoformat()

    # Replace all pending updates atomically
    await db.execute("DELETE FROM pending_updates")
    for pkg in packages:
        pi = info.get(pkg.name, query.PackageInfo(name=pkg.name))
        cats = categorizer.categories_str(pkg.name)
        trivial = categorizer.is_trivial(pkg.name)
        patch = categorizer.is_patch_update(pkg.old_version, pkg.new_version)
        in_news = pkg.name in news_pkgs
        in_garuda = pkg.name in garuda_pkgs

        # Security: worst severity across all vulnerable advisories for this package
        pkg_advisories = vuln_map.get(pkg.name, [])
        sec_severity = sec_mod.worst_severity(pkg_advisories) if pkg_advisories else ""

        is_flagged = pkg.name in flagged_set
        dep_count = dep_counts.get(pkg.name, 0)

        score, flags = risk_mod.score_package(
            pkg.name, pkg.old_version, pkg.new_version,
            in_news=in_news, hw=hw,
            security_severity=sec_severity or None,
            is_flagged=is_flagged,
            dep_count=dep_count,
            in_garuda_news=in_garuda,
        )

        await db.execute(
            """INSERT INTO pending_updates
               (package_name, old_version, new_version, description, url,
                old_date, new_date, category, is_trivial, is_patch,
                in_news, risk_score, risk_flags, security_severity,
                is_flagged, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pkg.name, pkg.old_version, pkg.new_version,
             pi.description, pi.url, pi.build_date, pi.install_date,
             cats, int(trivial), int(patch),
             int(in_news), score, json.dumps(flags),
             sec_severity, int(is_flagged), now),
        )
    await db.commit()
    return len(packages)


async def _get_news_items(db) -> list[dict]:
    """Get recent news items that mention packages in pending updates."""
    rows = await db.fetchall("""
        SELECT n.title, n.link, n.published_at, n.mentioned_packages
        FROM news n
        ORDER BY n.published_at DESC
        LIMIT 10
    """)
    items = []
    for row in rows:
        pkgs = (row["mentioned_packages"] or "").split("|")
        pkgs = [p for p in pkgs if p]
        items.append({
            "title": row["title"],
            "link": row["link"],
            "published_at": row["published_at"],
            "mentioned_packages": pkgs,
        })
    return items


def _short_date(pacman_date: str) -> str:
    """Shorten 'Sat 24 Jan 2026 01:47:26 CET' to '24 Jan 2026'."""
    if not pacman_date:
        return ""
    parts = pacman_date.split()
    # Format: DayOfWeek DD Mon YYYY HH:MM:SS TZ
    if len(parts) >= 4:
        return f"{parts[1]} {parts[2]} {parts[3]}"
    return pacman_date


async def _build_context(db):
    """Shared logic: fetch updates + compute stats for template."""
    updates = await db.fetchall("SELECT * FROM pending_updates" + _ORDER_SQL)
    pkgs = [dict(row) for row in updates]

    # Shorten pacman date strings for display
    for p in pkgs:
        p["old_date"] = _short_date(p.get("old_date") or "")
        p["new_date"] = _short_date(p.get("new_date") or "")

    total = len(pkgs)
    trivial_count = sum(1 for p in pkgs if p["is_trivial"])
    patch_count = sum(1 for p in pkgs if p["is_patch"])
    news_count = sum(1 for p in pkgs if p["in_news"])
    cve_count = sum(1 for p in pkgs if p.get("security_severity"))
    flagged_count = sum(1 for p in pkgs if p.get("is_flagged"))
    cat_counts = {}
    for cat in ("graphics", "kernel", "system", "mesa", "xorg"):
        cat_counts[cat] = sum(1 for p in pkgs if cat in (p["category"] or "").split())

    # Aggregate risk summary
    high_risk = sum(1 for p in pkgs if p["risk_score"] >= 40)
    max_risk = max((p["risk_score"] for p in pkgs), default=0)

    return {
        "updates": pkgs,
        "total": total,
        "trivial_count": trivial_count,
        "patch_count": patch_count,
        "news_count": news_count,
        "cve_count": cve_count,
        "flagged_count": flagged_count,
        "cat_counts": cat_counts,
        "high_risk_count": high_risk,
        "max_risk": max_risk,
    }


@router.get("/preview")
async def preview(request: Request):
    db = request.app.state.db
    templates = request.app.state.templates

    # Check if we have cached results
    row = await db.fetchone("SELECT COUNT(*) as cnt, MIN(checked_at) as checked FROM pending_updates")
    has_data = row["cnt"] > 0
    checked_at = row["checked"] if has_data else None

    # If no data yet, do a fresh check
    if not has_data:
        count = await _refresh_pending(db)
        if count > 0:
            checked_at = datetime.now(timezone.utc).isoformat()

    ctx = await _build_context(db)
    news_items = await _get_news_items(db)

    # Filter news to only items mentioning packages in the pending list,
    # and trim tags to only the matching packages
    pending_names = {p["package_name"] for p in ctx["updates"]}
    relevant_news = []
    for n in news_items:
        matching = [pkg for pkg in n["mentioned_packages"] if pkg in pending_names]
        if matching:
            relevant_news.append({**n, "mentioned_packages": matching})

    return templates.TemplateResponse(request, "preview.html", {
        "request": request,
        "active_page": "preview",
        "checked_at": checked_at,
        "relevant_news": relevant_news,
        **ctx,
    })


@router.post("/htmx/preview-refresh")
async def preview_refresh(request: Request):
    """HTMX: trigger a fresh checkupdates run and return updated table."""
    db = request.app.state.db
    config = request.app.state.config
    templates = request.app.state.templates

    # Pick up any new transactions from pacman.log first
    await _import_pacman_log(db, config)
    await _refresh_pending(db)
    ctx = await _build_context(db)

    return templates.TemplateResponse(request, "preview_table.html", {
        "request": request,
        **ctx,
    })


_UPGRADE_CMDS = {
    "garuda-update": "garuda-update",
    "pacman-syu": "sudo pacman -Syu",
}

_TERMINALS = [
    ("konsole", ["-e"]),
    ("kitty", []),
    ("alacritty", ["-e"]),
    ("xterm", ["-e"]),
]


def _find_terminal() -> tuple[str, list[str]] | None:
    """Find an available terminal emulator."""
    for name, args in _TERMINALS:
        if shutil.which(name):
            return name, args
    return None


@router.post("/htmx/upgrade-launch")
async def upgrade_launch(request: Request, cmd: str = ""):
    """Auto-backup DB then launch upgrade command in a terminal."""
    cmd_key = cmd
    cmd = _UPGRADE_CMDS.get(cmd_key)
    if not cmd:
        return HTMLResponse(
            '<span style="color: var(--warning);">Unknown command.</span>',
            status_code=400,
        )

    db = request.app.state.db
    db_path = request.app.state.config.db_path

    # Auto-backup before upgrade
    backup_name = await create_backup(db, db_path)

    # Find and launch terminal
    term = _find_terminal()
    if not term:
        return HTMLResponse(
            f'<span style="color: var(--green);">Backup created: {backup_name}</span>'
            f'<br><span style="color: var(--warning);">No terminal emulator found. Run manually: <code>{cmd}</code></span>'
        )

    term_bin, term_args = term
    shell_cmd = f'{cmd}; echo ""; echo "Done. Press Enter to close."; read'
    argv = [term_bin, *term_args, "bash", "-c", shell_cmd]

    # Launch terminal (spawn but don't wait for it to finish)
    await asyncio.create_subprocess_exec(*argv)

    return HTMLResponse(
        f'<span style="color: var(--green);">Backup created: {backup_name}</span>'
        f'<br><span style="color: var(--text-muted);">Terminal opened with <code>{cmd}</code></span>'
    )
