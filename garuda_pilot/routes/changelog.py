"""Changelog route — git history in dev, GitHub releases when installed."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from fastapi import APIRouter, Request

router = APIRouter()

_GITHUB_RELEASES_URL = "https://api.github.com/repos/interceptor/garuda-pilot/releases"
_releases_cache: tuple[list[dict], float] | None = None  # (data, fetched_at)

# Project root (where .git lives)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class Commit:
    hash: str
    short_hash: str
    author: str
    date: str  # YYYY-MM-DD
    datetime: str  # full ISO
    subject: str
    body: str = ""
    features: list[str] = field(default_factory=list)
    co_authors: list[str] = field(default_factory=list)


async def _read_git_log() -> list[Commit]:
    """Read git log and parse into structured commits."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "log", "--all",
            "--format=%H%x00%h%x00%an%x00%ai%x00%s%x00%b%x01",
            cwd=str(_PROJECT_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
    except (FileNotFoundError, OSError):
        return []

    if proc.returncode != 0:
        return []

    text = stdout.decode("utf-8", errors="replace")
    entries = text.split("\x01")
    commits: list[Commit] = []

    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("\x00")
        if len(parts) < 5:
            continue

        full_hash, short_hash, author, datetime_str, subject = parts[:5]
        body = parts[5] if len(parts) > 5 else ""

        # Parse date from datetime (YYYY-MM-DD from "2026-02-12 22:44:53 +0100")
        date = datetime_str[:10]

        # Extract feature bullet points from body
        features: list[str] = []
        co_authors: list[str] = []
        clean_body_lines: list[str] = []

        for line in body.strip().splitlines():
            stripped = line.strip()
            if stripped.startswith("Co-Authored-By:"):
                co_authors.append(stripped.replace("Co-Authored-By:", "").strip())
            elif stripped.startswith("- "):
                features.append(stripped[2:])
            elif stripped:
                clean_body_lines.append(stripped)

        clean_body = "\n".join(clean_body_lines)

        commits.append(Commit(
            hash=full_hash,
            short_hash=short_hash,
            author=author,
            date=date,
            datetime=datetime_str,
            subject=subject,
            body=clean_body,
            features=features,
            co_authors=co_authors,
        ))

    return commits


def _group_by_date(commits: list[Commit]) -> list[tuple[str, list[Commit]]]:
    """Group commits by date, ordered newest first."""
    groups: dict[str, list[Commit]] = {}
    for c in commits:
        groups.setdefault(c.date, []).append(c)
    return sorted(groups.items(), key=lambda x: x[0], reverse=True)


async def _fetch_github_releases() -> list[dict]:
    """Fetch releases from GitHub API, cached for 1 hour."""
    global _releases_cache
    if _releases_cache and time.monotonic() - _releases_cache[1] < 3600:
        return _releases_cache[0]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                _GITHUB_RELEASES_URL,
                headers={"Accept": "application/vnd.github.v3+json"},
            )
            if resp.status_code == 200:
                releases = [
                    {
                        "tag": r["tag_name"],
                        "name": r["name"] or r["tag_name"],
                        "date": r["published_at"][:10],
                        "body": (r["body"] or "").strip(),
                        "url": r["html_url"],
                    }
                    for r in resp.json()
                ]
                _releases_cache = (releases, time.monotonic())
                return releases
    except Exception:
        pass
    return []


@router.get("/changelog")
async def changelog_page(request: Request):
    templates = request.app.state.templates

    commits = await _read_git_log()
    grouped = _group_by_date(commits)
    releases = []

    if not commits:
        releases = await _fetch_github_releases()

    return templates.TemplateResponse(request, "changelog.html", {
        "request": request,
        "active_page": "changelog",
        "grouped_commits": grouped,
        "total_commits": len(commits),
        "releases": releases,
    })
