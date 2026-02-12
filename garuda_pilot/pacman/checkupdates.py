"""Async wrapper around the `checkupdates` command."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class PendingPackage:
    """A single pending update from checkupdates output."""
    name: str
    old_version: str
    new_version: str


async def check_updates() -> list[PendingPackage]:
    """Run checkupdates and parse output into PendingPackage list.

    Returns an empty list when no updates are available (exit code 2)
    or checkupdates is not installed.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "checkupdates",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
    except FileNotFoundError:
        return []

    # Exit code 0 = updates available, 2 = no updates, 1 = error
    if proc.returncode not in (0, 2):
        return []
    if proc.returncode == 2 or not stdout:
        return []

    packages = []
    for line in stdout.decode().strip().splitlines():
        # Format: "package_name old_version -> new_version"
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "->":
            packages.append(PendingPackage(
                name=parts[0],
                old_version=parts[1],
                new_version=parts[3],
            ))
    return packages
