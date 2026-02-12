"""Package categorization, trivial detection, and patch detection.

Ported from garuda-upgrade-preview-html-v2.sh categorize_package(),
is_trivial(), and is_patch_update() functions.
"""

from __future__ import annotations

import re

CATEGORY_PATTERNS: dict[str, re.Pattern] = {
    "graphics": re.compile(r"(nvidia|amdgpu|intel-media|vulkan|opencl)"),
    "kernel": re.compile(r"^(linux|linux-headers|linux-firmware)(\b|-)"),
    "system": re.compile(r"(^|\b)(systemd|glibc|gcc|pacman|pam|sudo|dbus)(\b|-)"),
    "mesa": re.compile(r"(mesa|lib32-mesa|libgl|libglvnd)"),
    "xorg": re.compile(r"(xorg|wayland|plasma|gnome-shell|mutter)"),
}

TRIVIAL_PATTERNS: list[re.Pattern] = [
    re.compile(r"(-docs?$|^man-|^texinfo|^info-|^help)"),
    re.compile(r"(^ttf-|^otf-|^noto-fonts|-fonts?$|^fonts?-)"),
    re.compile(r"(-themes?$|^themes?-|-icons?-|^icons?-|^cursor)"),
    re.compile(r"(-i18n|-l10n|^lang-|^language-|^locale-|^translate)"),
]


def categorize(name: str) -> list[str]:
    """Return list of categories a package belongs to."""
    categories = []
    for cat, pattern in CATEGORY_PATTERNS.items():
        if pattern.search(name):
            categories.append(cat)
    return categories


def categories_str(name: str) -> str:
    """Return space-separated category string (for DB storage)."""
    cats = categorize(name)
    return " ".join(cats) if cats else ""


def is_trivial(name: str) -> bool:
    """Check if a package is trivial (docs, fonts, themes, translations)."""
    return any(p.search(name) for p in TRIVIAL_PATTERNS)


def is_patch_update(old_version: str, new_version: str) -> bool:
    """Check if only the rebuild number changed (everything before last hyphen is the same)."""
    old_base = old_version.rsplit("-", 1)[0]
    new_base = new_version.rsplit("-", 1)[0]
    return old_base == new_base
