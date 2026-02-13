"""Arch Security Tracker integration.

Fetches vulnerability groups (AVGs) from
https://security.archlinux.org/issues/all.json

Each AVG contains affected packages, severity, status, CVEs, and
affected/fixed version info.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

SECURITY_URL = "https://security.archlinux.org/issues/all.json"
FETCH_TIMEOUT = 20

# Severity ordering (worst first)
SEVERITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Unknown": 4}


@dataclass
class SecurityAdvisory:
    """A single Arch vulnerability group (AVG)."""
    name: str                     # e.g. "AVG-2843"
    packages: list[str]           # affected package names
    status: str                   # "Vulnerable", "Fixed", "Unknown", "Not affected"
    severity: str                 # "Critical", "High", "Medium", "Low", "Unknown"
    type: str = ""                # e.g. "arbitrary code execution"
    affected: str = ""            # affected version string
    fixed: str = ""               # fixed version string (empty if not yet fixed)
    cves: list[str] = field(default_factory=list)


async def fetch_advisories() -> list[SecurityAdvisory]:
    """Fetch all AVGs from the Arch Security Tracker."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(SECURITY_URL, timeout=FETCH_TIMEOUT)
            resp.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException):
        return []

    try:
        data = resp.json()
    except (ValueError, TypeError):
        return []

    advisories = []
    for item in data:
        advisories.append(SecurityAdvisory(
            name=item.get("name", ""),
            packages=item.get("packages", []),
            status=item.get("status", "Unknown"),
            severity=item.get("severity", "Unknown"),
            type=item.get("type", ""),
            affected=item.get("affected", ""),
            fixed=item.get("fixed") or "",
            cves=item.get("issues", []),
        ))
    return advisories


async def store_advisories(db, advisories: list[SecurityAdvisory]) -> None:
    """Store advisories in the database, deduplicating by AVG name."""
    now = datetime.now(timezone.utc).isoformat()
    for adv in advisories:
        await db.execute(
            """INSERT OR REPLACE INTO security_advisories
               (name, packages, status, severity, type, affected, fixed, cves, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (adv.name, "|".join(adv.packages), adv.status, adv.severity,
             adv.type, adv.affected, adv.fixed, "|".join(adv.cves), now),
        )
    await db.commit()


async def ensure_advisories(db) -> None:
    """Fetch and store advisories if stale (>2h)."""
    row = await db.fetchone(
        "SELECT fetched_at FROM security_advisories ORDER BY fetched_at DESC LIMIT 1"
    )
    if row:
        try:
            fetched = datetime.fromisoformat(row["fetched_at"])
            if datetime.now(timezone.utc) - fetched < timedelta(hours=2):
                return
        except (ValueError, TypeError):
            pass

    advisories = await fetch_advisories()
    if advisories:
        await store_advisories(db, advisories)


async def get_vulnerable_packages(db) -> dict[str, list[dict]]:
    """Get packages with Vulnerable status.

    Returns dict mapping package_name -> list of advisory dicts with
    severity, type, cve_count, and avg_name.
    """
    rows = await db.fetchall(
        "SELECT * FROM security_advisories WHERE status = 'Vulnerable'"
    )
    vuln_map: dict[str, list[dict]] = {}
    for row in rows:
        pkgs = (row["packages"] or "").split("|")
        cves = (row["cves"] or "").split("|")
        cves = [c for c in cves if c]
        info = {
            "name": row["name"],
            "severity": row["severity"],
            "type": row["type"],
            "cve_count": len(cves),
            "affected": row["affected"],
        }
        for pkg in pkgs:
            pkg = pkg.strip()
            if pkg:
                vuln_map.setdefault(pkg, []).append(info)
    return vuln_map


def worst_severity(advisories: list[dict]) -> str:
    """Return the worst severity from a list of advisory dicts."""
    if not advisories:
        return ""
    best = min(advisories, key=lambda a: SEVERITY_ORDER.get(a["severity"], 99))
    return best["severity"]


async def get_all_advisories(db) -> list[dict]:
    """Get all advisories for the security page."""
    rows = await db.fetchall(
        "SELECT * FROM security_advisories ORDER BY name DESC"
    )
    items = []
    for row in rows:
        pkgs = (row["packages"] or "").split("|")
        pkgs = [p.strip() for p in pkgs if p.strip()]
        cves = (row["cves"] or "").split("|")
        cves = [c.strip() for c in cves if c.strip()]
        items.append({
            "name": row["name"],
            "packages": pkgs,
            "status": row["status"],
            "severity": row["severity"],
            "type": row["type"],
            "affected": row["affected"],
            "fixed": row["fixed"],
            "cves": cves,
            "cve_count": len(cves),
        })
    return items
