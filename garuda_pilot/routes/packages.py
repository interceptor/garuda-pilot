"""Packages page — all explicitly installed packages with reinstall commands."""

from __future__ import annotations

import asyncio
from fastapi import APIRouter, Request
from ..pacman import query

router = APIRouter()


async def _get_explicit_packages() -> set[str]:
    """Return set of explicitly installed package names via pacman -Qe."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pacman", "-Qe",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        return {line.split()[0] for line in stdout.decode().splitlines() if line.strip()}
    except Exception:
        return set()


@router.get("/packages")
async def packages_page(request: Request):
    db = request.app.state.db
    templates = request.app.state.templates

    # Get all install and remove operations with their transaction source/type
    rows = await db.fetchall("""
        SELECT
            po.package_name,
            po.action,
            po.new_version,
            po.category,
            t.source,
            t.started_at,
            (SELECT tl.message FROM transaction_logs tl
             WHERE tl.transaction_id = t.id AND tl.log_type = 'command'
             LIMIT 1) as pacman_command
        FROM package_operations po
        JOIN transactions t ON t.id = po.transaction_id
        WHERE po.action IN ('installed', 'removed')
        ORDER BY t.started_at ASC
    """)

    # Build current package state: installed minus removed
    # Track per source so we can generate correct reinstall commands
    pkg_state: dict[str, dict] = {}

    for row in rows:
        name = row["package_name"]
        source = row["source"] or "log"
        cmd = row["pacman_command"]

        if source == "flatpak":
            pkg_source = "flatpak"
        elif source == "pipx":
            pkg_source = "pipx"
        elif source == "log":
            from .history import _classify_command
            cmd_type = _classify_command(cmd)
            if cmd_type in ("system-upgrade", "garuda-internal", "mhwd"):
                pkg_source = "system"
            elif cmd_type in ("aur-install",):
                pkg_source = "aur"
            else:
                pkg_source = "pacman"
        else:
            pkg_source = "pacman"

        if row["action"] == "installed":
            pkg_state[name] = {
                "name": name,
                "version": row["new_version"],
                "source": pkg_source,
                "installed_at": row["started_at"],
                "category": row["category"],
            }
        elif row["action"] == "removed" and name in pkg_state:
            del pkg_state[name]

    # Filter native packages against pacman -Qe to remove transitive deps
    explicit = await _get_explicit_packages()

    # Group by source
    groups: dict[str, list] = {
        "pacman": [],
        "aur": [],
        "flatpak": [],
        "pipx": [],
        "system": [],
    }
    for pkg in sorted(pkg_state.values(), key=lambda p: p["name"]):
        src = pkg["source"]
        if src in ("pacman", "aur", "system") and pkg["name"] not in explicit:
            continue
        groups[src].append(pkg)

    # Fetch descriptions for native packages
    native_names = [p["name"] for p in groups["pacman"]] + [p["name"] for p in groups["aur"]]
    if native_names:
        pkg_info = await query.bulk_query(native_names)
        for src in ("pacman", "aur"):
            for pkg in groups[src]:
                info = pkg_info.get(pkg["name"])
                if info:
                    pkg["description"] = info.description
                    pkg["url"] = info.url

    # Generate reinstall commands
    reinstall_commands = {}
    if groups["pacman"]:
        names = " ".join(p["name"] for p in groups["pacman"])
        reinstall_commands["pacman"] = f"paru -S {names}"
    if groups["aur"]:
        names = " ".join(p["name"] for p in groups["aur"])
        reinstall_commands["aur"] = f"paru -S {names}"
    if groups["flatpak"]:
        ids = " ".join(p["name"] for p in groups["flatpak"])
        reinstall_commands["flatpak"] = f"flatpak install flathub {ids}"
    if groups["pipx"]:
        cmds = " && ".join(f"pipx install {p['name']}" for p in groups["pipx"])
        reinstall_commands["pipx"] = cmds

    shown_total = sum(len(groups[s]) for s in ("pacman", "aur", "flatpak", "pipx"))

    return templates.TemplateResponse("packages.html", {
        "request": request,
        "active_page": "packages",
        "groups": groups,
        "reinstall_commands": reinstall_commands,
        "total": shown_total,
    })
