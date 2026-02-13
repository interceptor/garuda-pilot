"""Arch Linux Package API integration.

Fetches per-package metadata from archlinux.org/packages/search/json/
to get flag_date (out-of-date) and dependency info.

Source: https://archlinux.org/packages/search/json/?q={name}
Per-package: https://archlinux.org/packages/{repo}/{arch}/{pkg}/json/
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx

SEARCH_URL = "https://archlinux.org/packages/search/json/"
FETCH_TIMEOUT = 15
BATCH_DELAY = 0.5  # seconds between batch requests


@dataclass
class PackageMeta:
    """Metadata for a single package from the Arch API."""
    name: str
    repo: str = ""
    flag_date: str | None = None
    maintainers: list[str] = field(default_factory=list)
    depends: list[str] = field(default_factory=list)


async def fetch_package_meta(names: list[str]) -> dict[str, PackageMeta]:
    """Fetch metadata for a list of package names.

    Uses the search API to batch-fetch results. Queries in groups of
    packages with delays to respect rate limits.

    Returns dict mapping package name -> PackageMeta.
    """
    if not names:
        return {}

    results: dict[str, PackageMeta] = {}
    seen_names = set()

    async with httpx.AsyncClient() as client:
        # Query in batches — search API can handle one name at a time
        # but we can search for exact names efficiently
        for i, name in enumerate(names):
            if name in seen_names:
                continue
            seen_names.add(name)

            if i > 0 and i % 10 == 0:
                await asyncio.sleep(BATCH_DELAY)

            try:
                resp = await client.get(
                    SEARCH_URL,
                    params={"name": name, "arch": "x86_64"},
                    timeout=FETCH_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, httpx.TimeoutException, ValueError):
                continue

            for pkg in data.get("results", []):
                pkg_name = pkg.get("pkgname", "")
                if pkg_name == name:
                    results[pkg_name] = PackageMeta(
                        name=pkg_name,
                        repo=pkg.get("repo", ""),
                        flag_date=pkg.get("flag_date"),
                        maintainers=pkg.get("maintainers", []),
                        depends=pkg.get("depends", []),
                    )
                    break

    return results


def get_flagged_packages(metas: dict[str, PackageMeta]) -> set[str]:
    """Return names of packages that are flagged out-of-date."""
    return {name for name, meta in metas.items() if meta.flag_date}


def compute_dep_counts(
    metas: dict[str, PackageMeta],
    pending_names: set[str],
) -> dict[str, int]:
    """Count how many other pending packages depend on each pending package.

    This approximates "blast radius" — a package depended on by many
    others that are also being updated is higher risk.
    """
    dep_counts: dict[str, int] = {name: 0 for name in pending_names}

    for name, meta in metas.items():
        if name not in pending_names:
            continue
        for dep in meta.depends:
            # Strip version constraints like "glibc>=2.38"
            dep_name = dep.split(">=")[0].split("<=")[0].split("=")[0].split(">")[0].split("<")[0]
            if dep_name in dep_counts:
                dep_counts[dep_name] += 1

    return dep_counts
