"""Parse pipx package list into transaction-like structs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class PipxOperation:
    action: str       # always "installed"
    package_name: str
    version: str


@dataclass
class PipxSnapshot:
    started_at: str
    operations: list[PipxOperation] = field(default_factory=list)


async def get_pipx_packages() -> list[tuple[str, str]]:
    """Return list of (package_name, version) for installed pipx packages."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pipx", "list", "--short",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except FileNotFoundError:
        return []

    packages = []
    for line in stdout.decode(errors="replace").splitlines():
        # Format: "package 1.2.3"
        parts = line.strip().split()
        if len(parts) >= 2:
            packages.append((parts[0], parts[1]))
    return packages


async def build_pipx_snapshot(known_packages: set[str]) -> PipxSnapshot | None:
    """Build a snapshot of new pipx packages not already in known_packages.

    Returns None if nothing new.
    """
    packages = await get_pipx_packages()
    new_ops = [
        PipxOperation(action="installed", package_name=name, version=version)
        for name, version in packages
        if name not in known_packages
    ]
    if not new_ops:
        return None

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return PipxSnapshot(started_at=now, operations=new_ops)
