"""About / Help route — explains how garuda-pilot works, plus DB backup/restore."""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..db import SCHEMA_VERSION

router = APIRouter()

MAX_BACKUPS = 5

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_README_PATH = _PROJECT_ROOT / "README.md"


def _render_markdown_to_html(md: str) -> str:
    """Minimal Markdown-to-HTML converter for the README.

    Handles: headings, paragraphs, code blocks (fenced), inline code,
    bold, italic, links, unordered lists, ordered lists, tables,
    and horizontal rules. No external dependencies.
    """
    lines = md.split("\n")
    html_parts: list[str] = []
    in_code_block = False
    in_list = False
    in_ol = False
    in_table = False
    i = 0

    while i < len(lines):
        line = lines[i]

        # Fenced code blocks
        if line.strip().startswith("```"):
            if in_code_block:
                html_parts.append("</code></pre>")
                in_code_block = False
            else:
                lang = line.strip()[3:].strip()
                cls = f' class="lang-{lang}"' if lang else ""
                html_parts.append(f"<pre><code{cls}>")
                in_code_block = True
            i += 1
            continue

        if in_code_block:
            escaped = (line.replace("&", "&amp;")
                          .replace("<", "&lt;")
                          .replace(">", "&gt;"))
            html_parts.append(escaped)
            i += 1
            continue

        stripped = line.strip()

        # Close list if we're no longer in one
        if in_list and not stripped.startswith("- ") and not stripped.startswith("* "):
            html_parts.append("</ul>")
            in_list = False
        if in_ol and not re.match(r"^\d+\.\s", stripped):
            html_parts.append("</ol>")
            in_ol = False

        # Table detection
        if "|" in stripped and not in_table:
            # Check if next line is a separator
            if i + 1 < len(lines) and re.match(r"^\|[-\s|:]+\|$", lines[i + 1].strip()):
                in_table = True
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                html_parts.append("<table><thead><tr>")
                for cell in cells:
                    html_parts.append(f"<th>{_inline(cell)}</th>")
                html_parts.append("</tr></thead><tbody>")
                i += 2  # skip header + separator
                continue

        if in_table:
            if "|" in stripped and stripped.startswith("|"):
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                html_parts.append("<tr>")
                for cell in cells:
                    html_parts.append(f"<td>{_inline(cell)}</td>")
                html_parts.append("</tr>")
                i += 1
                continue
            else:
                html_parts.append("</tbody></table>")
                in_table = False

        # Empty line
        if not stripped:
            i += 1
            continue

        # Horizontal rule
        if re.match(r"^[-*_]{3,}$", stripped):
            html_parts.append("<hr>")
            i += 1
            continue

        # Headings
        m = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if m:
            level = len(m.group(1))
            text = _inline(m.group(2))
            html_parts.append(f"<h{level}>{text}</h{level}>")
            i += 1
            continue

        # Unordered list
        if stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            text = _inline(stripped[2:])
            html_parts.append(f"<li>{text}</li>")
            i += 1
            continue

        # Ordered list
        m = re.match(r"^(\d+)\.\s+(.*)", stripped)
        if m:
            if not in_ol:
                html_parts.append("<ol>")
                in_ol = True
            text = _inline(m.group(2))
            html_parts.append(f"<li>{text}</li>")
            i += 1
            continue

        # Paragraph
        html_parts.append(f"<p>{_inline(stripped)}</p>")
        i += 1

    # Close any open elements
    if in_list:
        html_parts.append("</ul>")
    if in_ol:
        html_parts.append("</ol>")
    if in_table:
        html_parts.append("</tbody></table>")
    if in_code_block:
        html_parts.append("</code></pre>")

    return "\n".join(html_parts)


def _inline(text: str) -> str:
    """Convert inline markdown: bold, italic, code, links."""
    # Escape HTML entities (but preserve already-valid tags)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Inline code
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # Bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # Italic
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    # Links
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" target="_blank">\1</a>', text)
    return text


def _load_readme() -> str:
    """Load and render README.md as HTML."""
    if _README_PATH.exists():
        md = _README_PATH.read_text(encoding="utf-8")
        return _render_markdown_to_html(md)
    return "<p>README.md not found.</p>"


def _list_backups(db_path: Path) -> list[dict]:
    """List existing backup files, newest first."""
    backup_dir = db_path.parent
    pattern = db_path.stem + ".backup-*" + db_path.suffix
    backups = sorted(backup_dir.glob(pattern), reverse=True)
    result = []
    for p in backups:
        # Extract timestamp and version from filename
        # New: garuda-pilot.backup-2026-02-13T14:30:00-v4.db
        # Old: garuda-pilot.backup-2026-02-13T14:30:00.db
        name = p.stem
        suffix = name.split(".backup-", 1)[-1] if ".backup-" in name else ""
        # Parse version suffix
        m = re.match(r"^(.+?)(?:-pre-restore)?(?:-(v\d+))?$", suffix)
        ts = m.group(1).replace("T", " ") if m else suffix.replace("T", " ")
        version = m.group(2) if m and m.group(2) else None
        stat = p.stat()
        size_mb = stat.st_size / (1024 * 1024)
        result.append({
            "filename": p.name,
            "timestamp": ts,
            "version": version,
            "size_mb": f"{size_mb:.1f}",
        })
    return result


def _prune_old_backups(db_path: Path) -> int:
    """Delete oldest backups beyond MAX_BACKUPS. Returns number deleted."""
    backup_dir = db_path.parent
    pattern = db_path.stem + ".backup-*" + db_path.suffix
    backups = sorted(backup_dir.glob(pattern), reverse=True)
    deleted = 0
    for old in backups[MAX_BACKUPS:]:
        old.unlink()
        deleted += 1
    return deleted


async def create_backup(db, db_path: Path) -> str:
    """Create a timestamped, schema-versioned backup. Returns the backup filename."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    backup_name = f"{db_path.stem}.backup-{ts}-v{SCHEMA_VERSION}{db_path.suffix}"
    backup_path = db_path.parent / backup_name

    try:
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass

    shutil.copy2(str(db_path), str(backup_path))
    _prune_old_backups(db_path)
    return backup_name


@router.get("/about")
async def about_page(request: Request):
    templates = request.app.state.templates
    db_path = request.app.state.config.db_path

    readme_html = _load_readme()
    backups = _list_backups(db_path)
    db_size_mb = f"{db_path.stat().st_size / (1024 * 1024):.1f}" if db_path.exists() else "0"

    return templates.TemplateResponse(request, "about.html", {
        "request": request,
        "active_page": "about",
        "readme_html": readme_html,
        "backups": backups,
        "db_size_mb": db_size_mb,
        "db_path": str(db_path),
        "max_backups": MAX_BACKUPS,
    })


@router.post("/htmx/backup-create")
async def backup_create(request: Request, brief: str = ""):
    """Create a timestamped backup of the database."""
    db_path: Path = request.app.state.config.db_path
    templates = request.app.state.templates

    if not db_path.exists():
        return HTMLResponse(
            '<span style="color: var(--warning);">Database file not found.</span>',
            status_code=404,
        )

    db = request.app.state.db
    backup_name = await create_backup(db, db_path)

    # Brief mode: return just a status message (used by dashboard)
    if brief:
        return HTMLResponse(
            f'<span style="color: var(--green); font-size: 0.85em;">Backup created</span>'
        )

    backups = _list_backups(db_path)
    db_size_mb = f"{db_path.stat().st_size / (1024 * 1024):.1f}"

    return templates.TemplateResponse(request, "backup_list.html", {
        "request": request,
        "backups": backups,
        "db_size_mb": db_size_mb,
        "db_path": str(db_path),
        "max_backups": MAX_BACKUPS,
        "backup_msg": f"Backup created: {backup_name}",
    })


@router.post("/htmx/backup-restore/{filename}")
async def backup_restore(filename: str, request: Request):
    """Restore the database from a backup file."""
    db_path: Path = request.app.state.config.db_path
    templates = request.app.state.templates

    # Validate filename to prevent path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        return HTMLResponse(
            '<div class="backup-msg backup-error">Invalid filename.</div>',
            status_code=400,
        )

    backup_path = db_path.parent / filename
    if not backup_path.exists():
        return HTMLResponse(
            '<div class="backup-msg backup-error">Backup file not found.</div>',
            status_code=404,
        )

    # Check schema version compatibility
    version_warning = ""
    m = re.search(r"-(v(\d+))\.", filename)
    if m:
        backup_ver = int(m.group(2))
        if backup_ver != SCHEMA_VERSION:
            version_warning = f" Warning: backup is schema v{backup_ver}, current is v{SCHEMA_VERSION} — the app may need to re-migrate."

    # Auto-backup current state before restoring
    db = request.app.state.db
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    pre_restore = db_path.parent / f"{db_path.stem}.backup-{ts}-pre-restore-v{SCHEMA_VERSION}{db_path.suffix}"

    try:
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass

    # Close the database connection before replacing the file
    await db.close()

    shutil.copy2(str(db_path), str(pre_restore))
    shutil.copy2(str(backup_path), str(db_path))

    # Reconnect to the restored database
    await db.connect()

    _prune_old_backups(db_path)
    backups = _list_backups(db_path)
    db_size_mb = f"{db_path.stat().st_size / (1024 * 1024):.1f}"

    return templates.TemplateResponse(request, "backup_list.html", {
        "request": request,
        "backups": backups,
        "db_size_mb": db_size_mb,
        "db_path": str(db_path),
        "max_backups": MAX_BACKUPS,
        "backup_msg": f"Restored from {filename} (pre-restore backup saved).{version_warning}",
    })
