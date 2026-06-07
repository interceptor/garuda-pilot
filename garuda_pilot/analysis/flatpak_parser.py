"""Parse flatpak history into transaction-like structs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime


# App IDs that are runtimes/platforms, not user-installed apps
_RUNTIME_PREFIXES = (
    "org.freedesktop.Platform",
    "org.freedesktop.Sdk",
    "org.kde.Platform",
    "org.kde.Sdk",
    "org.gnome.Platform",
    "org.gnome.Sdk",
    "com.valvesoftware.Steam.CompatibilityTool",
    "org.freedesktop.Platform.GL",
    "org.freedesktop.Platform.Locale",
    "org.freedesktop.Platform.codecs",
    "org.kde.Platform.Locale",
    "org.gtk.Gtk3theme",
)


@dataclass
class FlatpakOperation:
    action: str       # "installed" or "removed"
    app_id: str
    version: str


@dataclass
class FlatpakTransaction:
    started_at: str   # ISO-like string
    operations: list[FlatpakOperation] = field(default_factory=list)


def _is_runtime(app_id: str) -> bool:
    return any(app_id.startswith(p) for p in _RUNTIME_PREFIXES)


def _parse_flatpak_date(date_str: str) -> str | None:
    """Convert 'May 29 11:30:22' to ISO datetime using current year."""
    try:
        year = datetime.now().year
        dt = datetime.strptime(f"{date_str} {year}", "%b %d %H:%M:%S %Y")
        return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    except ValueError:
        return None


async def parse_flatpak_history(last_cursor: str | None = None) -> list[FlatpakTransaction]:
    """Run 'flatpak history' and parse into transactions.

    Each app install/remove event becomes its own transaction.
    Runtimes and platforms are filtered out.
    Only events newer than last_cursor (ISO string) are returned.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "flatpak", "history",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except FileNotFoundError:
        return []

    lines = stdout.decode(errors="replace").splitlines()
    transactions: list[FlatpakTransaction] = []

    for line in lines:
        parts = line.split("\t")
        if len(parts) < 4:
            continue

        date_str = parts[0].strip()
        event = parts[1].strip()
        app_id = parts[2].strip()

        if _is_runtime(app_id):
            continue

        if event == "deploy install":
            action = "installed"
        elif event == "deploy uninstall":
            action = "removed"
        else:
            continue

        iso = _parse_flatpak_date(date_str)
        if not iso:
            continue

        if last_cursor and iso <= last_cursor:
            continue

        transactions.append(FlatpakTransaction(
            started_at=iso,
            operations=[FlatpakOperation(action=action, app_id=app_id, version="")],
        ))

    return transactions


async def get_flatpak_apps() -> dict[str, str]:
    """Return {app_id: name} for currently installed flatpak apps."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "flatpak", "list", "--app", "--columns=application,name",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except FileNotFoundError:
        return {}

    result = {}
    for line in stdout.decode(errors="replace").splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            result[parts[0].strip()] = parts[1].strip()
    return result
