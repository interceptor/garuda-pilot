"""Changelog route — auto-generated from git history."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, Request

router = APIRouter()

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


@router.get("/changelog")
async def changelog_page(request: Request):
    templates = request.app.state.templates

    commits = await _read_git_log()
    grouped = _group_by_date(commits)

    return templates.TemplateResponse("changelog.html", {
        "request": request,
        "active_page": "changelog",
        "grouped_commits": grouped,
        "total_commits": len(commits),
    })
