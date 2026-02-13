"""Risk scoring engine for pending package updates.

Scores each package 0-100 based on category, news mentions,
hardware context, version bump magnitude, security advisories,
package flag status, and dependency blast radius.
"""

from __future__ import annotations

from ..pacman.categorizer import categorize, is_trivial, is_patch_update
from .hardware import HWProfile

# Category base weights
CATEGORY_WEIGHTS: dict[str, int] = {
    "kernel": 40,
    "graphics": 30,
    "system": 25,
    "mesa": 20,
    "xorg": 15,
}

# Security severity weights
# Source: https://security.archlinux.org/issues/all.json
SECURITY_WEIGHTS: dict[str, int] = {
    "Critical": 35,
    "High": 25,
    "Medium": 15,
    "Low": 5,
}


def score_package(
    name: str,
    old_version: str,
    new_version: str,
    *,
    in_news: bool = False,
    hw: HWProfile | None = None,
    security_severity: str | None = None,
    is_flagged: bool = False,
    dep_count: int = 0,
    in_garuda_news: bool = False,
) -> tuple[int, list[str]]:
    """Compute risk score (0-100) and list of risk flag strings.

    Returns (score, flags) where flags explain each risk component.
    """
    flags: list[str] = []
    score = 0

    trivial = is_trivial(name)
    patch = is_patch_update(old_version, new_version)

    # Trivial packages are capped at 5
    if trivial:
        return (5, ["trivial"])

    # Patch-only updates are capped at 10
    if patch:
        return (10, ["patch-only"])

    # Category weights (additive — a package can be in multiple categories)
    cats = categorize(name)
    for cat in cats:
        w = CATEGORY_WEIGHTS.get(cat, 0)
        if w:
            score += w
            flags.append(f"category:{cat}")

    # In Arch news (https://archlinux.org/feeds/news/)
    if in_news:
        score += 20
        flags.append("in-news")

    # In Garuda news (https://forum.garudalinux.org/c/announcements/16.rss)
    if in_garuda_news:
        score += 15
        flags.append("in-garuda-news")

    # Security advisory (https://security.archlinux.org/issues/all.json)
    if security_severity and security_severity in SECURITY_WEIGHTS:
        w = SECURITY_WEIGHTS[security_severity]
        score += w
        flags.append(f"cve-{security_severity.lower()}")

    # Flagged out-of-date on archlinux.org
    # (https://archlinux.org/packages/{repo}/{arch}/{pkg}/json/ — flag_date field)
    if is_flagged:
        score += 10
        flags.append("flagged-outdated")

    # High dependency count — many other pending packages depend on this one
    if dep_count > 10:
        score += 10
        flags.append("high-deps")

    # NVIDIA GPU + kernel update is dangerous
    if hw and hw.nvidia_module_loaded and "kernel" in cats:
        score += 30
        flags.append("nvidia+kernel")

    # Major version bump (first component changed)
    if _is_major_bump(old_version, new_version):
        score += 15
        flags.append("major-bump")

    # Ensure minimum score for non-trivial, non-patch packages
    if not flags:
        score = max(score, 5)

    return (min(score, 100), flags)


def risk_label(score: int) -> str:
    """Map a score to a human-readable label."""
    if score >= 60:
        return "critical"
    if score >= 40:
        return "high"
    if score >= 20:
        return "medium"
    return "low"


def _is_major_bump(old_version: str, new_version: str) -> bool:
    """Check if the major version component changed.

    Compares the first numeric segment before the first dot.
    e.g. "1.2.3-1" vs "2.0.0-1" -> True
         "1.2.3-1" vs "1.3.0-1" -> False
    """
    old_base = old_version.split("-")[0]  # strip pkgrel
    new_base = new_version.split("-")[0]
    old_parts = old_base.split(".")
    new_parts = new_base.split(".")
    if old_parts and new_parts:
        return old_parts[0] != new_parts[0]
    return False
