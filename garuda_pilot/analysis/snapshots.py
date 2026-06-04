"""Btrfs snapshot detection and listing via snapper."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Snapshot:
    number: int
    type: str           # single / pre / post
    pre_number: int | None
    date: str
    description: str
    cleanup: str = ""


@dataclass
class SnapshotStatus:
    tool: str           # "snapper" / "none"
    config: str         # snapper config name, e.g. "root"
    is_btrfs: bool
    snap_pac_active: bool
    snapshots: list[Snapshot] = field(default_factory=list)
    can_create: bool = False
    error: str = ""
    permission_hint: str = ""


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def _is_btrfs_root() -> bool:
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "/" and parts[2] == "btrfs":
                return True
    except OSError:
        pass
    return False


def _snap_pac_active() -> bool:
    hooks_dir = Path("/usr/share/libalpm/hooks")
    return any(hooks_dir.glob("*snap-pac*pre*"))


def _snapper_configs() -> list[str]:
    try:
        d = Path("/etc/snapper/configs")
        return [f.name for f in sorted(d.iterdir()) if f.is_file()]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Snapshot listing — snapper output then XML fallback
# ---------------------------------------------------------------------------

def _parse_snapper_text(text: str) -> list[Snapshot]:
    """Parse `snapper list` table output — text fallback for older snapper.

    Splits every line on │ or |, skips anything whose first field isn't
    an integer (header and separator rows). No separator-detection needed.
    """
    snapshots = []
    for line in text.splitlines():
        parts = [p.strip() for p in re.split(r"[│|]", line)]
        if len(parts) < 4:
            continue
        try:
            num = int(parts[0])
        except ValueError:
            continue
        pre_num = None
        try:
            pre_num = int(parts[2]) if parts[2] else None
        except ValueError:
            pass
        snapshots.append(Snapshot(
            number=num,
            type=parts[1],
            pre_number=pre_num,
            date=parts[3],
            description=parts[6] if len(parts) > 6 else "",
            cleanup=parts[5] if len(parts) > 5 else "",
        ))
    return snapshots


def _read_xml_snapshots() -> list[Snapshot]:
    """Fallback: parse /.snapshots/*/info.xml without snapper permissions."""
    snapshots = []
    base = Path("/.snapshots")
    try:
        entries = sorted(base.iterdir(), key=lambda p: p.name)
    except (OSError, PermissionError):
        return []
    for entry in entries:
        xml = entry / "info.xml"
        try:
            text = xml.read_text()
        except (OSError, PermissionError):
            continue
        get = lambda tag: (m.group(1) if (m := re.search(rf"<{tag}>([^<]*)</{tag}>", text)) else "")
        try:
            num = int(get("num"))
        except ValueError:
            continue
        pre_raw = get("pre_num")
        snapshots.append(Snapshot(
            number=num,
            type=get("type") or "single",
            pre_number=int(pre_raw) if pre_raw else None,
            date=get("date"),
            description=get("description"),
            cleanup=get("cleanup"),
        ))
    return list(reversed(snapshots))


async def _run_snapper_list(config: str) -> tuple[list[Snapshot], str]:
    """Returns (snapshots, error_message). error_message is '' on success.

    Tries --jsonout first (snapper 0.9+), falls back to text table parsing.
    """
    import json as _json

    for use_json in (True, False):
        args = ["snapper", "--jsonout", "-c", config, "list"] if use_json else \
               ["snapper", "-c", config, "list"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        except FileNotFoundError:
            return [], "snapper not found"
        except asyncio.TimeoutError:
            return [], "snapper timed out"

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            if use_json:
                continue  # try text fallback
            return [], err or f"exit {proc.returncode}"

        if use_json:
            try:
                data = _json.loads(stdout.decode())
                snaps = data.get(config, [])
                snapshots = [
                    Snapshot(
                        number=s["number"],
                        type=s.get("type", "single"),
                        pre_number=s.get("pre-number"),
                        date=s.get("date", ""),
                        description=s.get("description", ""),
                        cleanup=s.get("cleanup", ""),
                    )
                    for s in snaps
                ]
                return snapshots, ""
            except (_json.JSONDecodeError, KeyError):
                continue  # try text fallback
        else:
            return _parse_snapper_text(stdout.decode(errors="replace")), ""

    return [], "could not parse snapper output"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_status() -> SnapshotStatus:
    """Detect snapshot tools and list existing snapshots."""
    is_btrfs = _is_btrfs_root()

    if not shutil.which("snapper"):
        return SnapshotStatus(
            tool="none", config="", is_btrfs=is_btrfs,
            snap_pac_active=_snap_pac_active(),
            error="snapper not installed" + (" — install it with: sudo pacman -S snapper" if is_btrfs else ""),
        )

    configs = _snapper_configs()
    config = "root" if "root" in configs else (configs[0] if configs else "root")
    snap_pac = _snap_pac_active()

    snapshots, err = await _run_snapper_list(config)
    can_create = not err
    permission_hint = ""

    if err and ("permission" in err.lower() or not err):
        # Try XML fallback
        snapshots = _read_xml_snapshots()
        can_create = False
        user = os.environ.get("USER", "your_user")
        permission_hint = (
            f"To enable listing and creating snapshots from the web UI, "
            f"run: sudo snapper -c {config} set-config 'ALLOW_USERS={user}'"
        )
        err = ""  # Don't show raw error once we have the hint

    return SnapshotStatus(
        tool="snapper",
        config=config,
        is_btrfs=is_btrfs,
        snap_pac_active=snap_pac,
        snapshots=snapshots,
        can_create=can_create,
        error=err,
        permission_hint=permission_hint,
    )


async def create_snapshot(config: str, description: str, snap_type: str = "single") -> tuple[int | None, str]:
    """Create a snapshot. Returns (snapshot_number, error). Requires snapper permissions."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "snapper", "-c", config, "create",
            "--type", snap_type,
            "--description", description,
            "--print-number",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except FileNotFoundError:
        return None, "snapper not found"
    except asyncio.TimeoutError:
        return None, "timed out"

    if proc.returncode != 0:
        return None, stderr.decode(errors="replace").strip()

    try:
        num = int(stdout.decode().strip())
        return num, ""
    except ValueError:
        return None, ""


def pre_upgrade_shell_prefix(config: str) -> str:
    """Shell fragment to prepend to an upgrade command.

    Creates a pre snapshot, runs the upgrade, creates a matching post snapshot.
    The entire sequence is wrapped so a failed snapshot doesn't abort the upgrade.
    """
    return (
        f'SNAP_PRE=$(sudo snapper -c {config} create --type pre '
        f'--description "pre-upgrade (garuda-pilot)" --print-number 2>/dev/null); '
    )


def post_upgrade_shell_suffix(config: str) -> str:
    """Shell fragment to append after an upgrade command."""
    return (
        f'; if [ -n "$SNAP_PRE" ]; then '
        f'sudo snapper -c {config} create --type post --pre-number "$SNAP_PRE" '
        f'--description "post-upgrade (garuda-pilot)" 2>/dev/null; '
        f'echo "Snapshot pair created (pre=$SNAP_PRE)"; fi'
    )
