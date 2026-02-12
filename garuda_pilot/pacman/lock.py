"""Detect pacman database lock."""

from __future__ import annotations

from pathlib import Path

LOCK_PATH = Path("/var/lib/pacman/db.lck")


def is_pacman_locked() -> bool:
    """Check if another pacman process holds the database lock."""
    return LOCK_PATH.exists()
