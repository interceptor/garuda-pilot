"""Bulk pacman package info queries.

Runs `pacman -Qi` and `pacman -Si` once for all packages (2 subprocess
calls total instead of 2*N), then parses the multi-record output.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class PackageInfo:
    """Aggregated info from pacman -Qi and -Si for a single package."""
    name: str
    description: str = ""
    url: str = ""
    old_date: str = ""   # Build Date from installed (-Qi)
    new_date: str = ""   # Build Date from sync DB (-Si)


def _parse_multi_record(output: str, fields: set[str]) -> list[dict[str, str]]:
    """Parse multi-record pacman output into a list of dicts.

    Each record is separated by a blank line.  Fields are in
    "Key  : Value" format, where the key is left-padded with spaces.
    """
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}

    for line in output.splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        if " : " in line:
            key, _, value = line.partition(" : ")
            key = key.strip()
            if key in fields:
                current[key] = value.strip()

    if current:
        records.append(current)

    return records


async def _run_pacman(args: list[str], packages: list[str]) -> str:
    """Run a pacman command and return stdout."""
    if not packages:
        return ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pacman", *args, *packages,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode(errors="replace")
    except FileNotFoundError:
        return ""


async def bulk_query(packages: list[str]) -> dict[str, PackageInfo]:
    """Query installed (-Qi) and sync (-Si) info for all packages at once.

    Returns a dict keyed by package name.
    """
    if not packages:
        return {}

    # Run both queries in parallel
    qi_task = _run_pacman(["-Qi", "--"], packages)
    si_task = _run_pacman(["-Si", "--"], packages)
    qi_output, si_output = await asyncio.gather(qi_task, si_task)

    result: dict[str, PackageInfo] = {name: PackageInfo(name=name) for name in packages}

    # Parse installed info
    qi_fields = {"Name", "Description", "Build Date"}
    for record in _parse_multi_record(qi_output, qi_fields):
        name = record.get("Name", "")
        if name in result:
            result[name].description = record.get("Description", "")
            result[name].old_date = record.get("Build Date", "")

    # Parse sync info
    si_fields = {"Name", "Description", "URL", "Build Date"}
    for record in _parse_multi_record(si_output, si_fields):
        name = record.get("Name", "")
        if name in result:
            # Sync description as fallback if installed is empty
            if not result[name].description:
                result[name].description = record.get("Description", "")
            result[name].url = record.get("URL", "")
            result[name].new_date = record.get("Build Date", "")

    return result
