"""Hardware detection — GPU, kernel, nvidia module status.

Used by risk scoring to flag dangerous combos like nvidia + kernel update.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class HWProfile:
    """Detected hardware profile."""
    gpu_vendor: str = ""
    gpu_model: str = ""
    kernel: str = ""
    kernel_version: str = ""
    nvidia_module_loaded: bool = False


async def detect() -> HWProfile:
    """Detect GPU and kernel info from the running system."""
    profile = HWProfile()

    # Kernel info from uname
    try:
        proc = await asyncio.create_subprocess_exec(
            "uname", "-r",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            profile.kernel_version = stdout.decode().strip()
            # e.g. "6.18.7-zen1-1-zen" -> kernel is "linux-zen"
            kver = profile.kernel_version
            if "-zen" in kver:
                profile.kernel = "linux-zen"
            elif "-lts" in kver:
                profile.kernel = "linux-lts"
            elif "-hardened" in kver:
                profile.kernel = "linux-hardened"
            else:
                profile.kernel = "linux"
    except FileNotFoundError:
        pass

    # GPU from lspci
    try:
        proc = await asyncio.create_subprocess_exec(
            "lspci", "-nn",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            for line in stdout.decode().splitlines():
                lower = line.lower()
                if "vga" in lower or "3d controller" in lower or "display" in lower:
                    if "nvidia" in lower:
                        profile.gpu_vendor = "nvidia"
                        profile.gpu_model = line.split(":", 2)[-1].strip() if ":" in line else line
                    elif "amd" in lower or "ati" in lower:
                        profile.gpu_vendor = "amd"
                        profile.gpu_model = line.split(":", 2)[-1].strip() if ":" in line else line
                    elif "intel" in lower:
                        profile.gpu_vendor = "intel"
                        profile.gpu_model = line.split(":", 2)[-1].strip() if ":" in line else line
                    break  # Use the first GPU found
    except FileNotFoundError:
        pass

    # Check if nvidia kernel module is loaded
    modules_path = Path("/proc/modules")
    if modules_path.exists():
        try:
            text = modules_path.read_text()
            profile.nvidia_module_loaded = any(
                line.split()[0] == "nvidia" for line in text.splitlines() if line
            )
        except OSError:
            pass

    return profile


async def store_profile(db, profile: HWProfile) -> None:
    """Store hardware profile in DB (upsert singleton row)."""
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT OR REPLACE INTO hardware_profile
           (id, gpu_vendor, gpu_model, kernel, kernel_version,
            nvidia_module_loaded, detected_at)
           VALUES (1, ?, ?, ?, ?, ?, ?)""",
        (profile.gpu_vendor, profile.gpu_model, profile.kernel,
         profile.kernel_version, int(profile.nvidia_module_loaded), now),
    )
    await db.commit()


async def load_profile(db) -> HWProfile | None:
    """Load hardware profile from DB."""
    row = await db.fetchone("SELECT * FROM hardware_profile WHERE id = 1")
    if not row:
        return None
    return HWProfile(
        gpu_vendor=row["gpu_vendor"] or "",
        gpu_model=row["gpu_model"] or "",
        kernel=row["kernel"] or "",
        kernel_version=row["kernel_version"] or "",
        nvidia_module_loaded=bool(row["nvidia_module_loaded"]),
    )
