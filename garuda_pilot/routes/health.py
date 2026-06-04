"""Health routes — system health check display."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..analysis import health as health_mod

router = APIRouter()


@router.get("/health")
async def health_page(request: Request):
    db = request.app.state.db
    templates = request.app.state.templates

    # Load latest snapshot (if any)
    latest, checked_at = await health_mod.load_latest_snapshot(db)

    # If no snapshot exists, run a check now
    if latest is None:
        latest = await health_mod.run_health_check()
        await health_mod.store_snapshot(db, latest, source="auto")
        row = await db.fetchone(
            "SELECT checked_at FROM health_snapshots ORDER BY id DESC LIMIT 1"
        )
        checked_at = row["checked_at"] if row else None

    # Load history and comparison
    history = await health_mod.load_snapshot_history(db)
    comparison = await health_mod.compare_with_previous(db, latest)

    check_names = (health_mod.CHECK_NAMES if latest.backend == "garuda-health"
                   else health_mod.NATIVE_CHECK_NAMES)
    return templates.TemplateResponse(request, "health.html", {
        "request": request,
        "active_page": "health",
        "result": latest,
        "checked_at": checked_at,
        "history": history,
        "comparison": comparison,
        "check_names": check_names,
    })


@router.post("/htmx/health-refresh")
async def health_refresh(request: Request):
    """HTMX: run a fresh health check and return updated content."""
    db = request.app.state.db
    templates = request.app.state.templates

    result = await health_mod.run_health_check()
    await health_mod.store_snapshot(db, result, source="manual")

    row = await db.fetchone(
        "SELECT checked_at FROM health_snapshots ORDER BY id DESC LIMIT 1"
    )
    checked_at = row["checked_at"] if row else None
    history = await health_mod.load_snapshot_history(db)
    comparison = await health_mod.compare_with_previous(db, result)

    check_names = (health_mod.CHECK_NAMES if result.backend == "garuda-health"
                   else health_mod.NATIVE_CHECK_NAMES)
    return templates.TemplateResponse(request, "health_content.html", {
        "request": request,
        "result": result,
        "checked_at": checked_at,
        "history": history,
        "comparison": comparison,
        "check_names": check_names,
    })


@router.get("/htmx/journal-errors")
async def journal_errors(request: Request):
    """HTMX: fetch and display journal errors from the past 24h."""
    templates = request.app.state.templates
    config = request.app.state.config
    lines, total = await health_mod.fetch_journal_errors()
    reports = health_mod.list_journal_reports(config.journal_report_dir)
    return templates.TemplateResponse(request, "journal_errors.html", {
        "request": request,
        "lines": lines,
        "total": total,
        "has_claude": bool(config.claude_api_key),
        "has_ollama": bool(config.ollama_url),
        "ollama_model": config.ollama_model,
        "reports": reports,
    })


@router.post("/htmx/journal-analyze")
async def journal_analyze(request: Request, provider: str = "claude"):
    """HTMX: AI analysis of journal errors."""
    config = request.app.state.config
    lines, total = await health_mod.fetch_journal_errors()
    distro = health_mod._read_distro()

    if provider == "claude":
        if not config.claude_api_key:
            return HTMLResponse('<p style="color:var(--warning);">Claude API key not configured — go to <a href="/settings">Settings</a>.</p>')
        result = await health_mod.analyze_journal_claude(lines, total, config.claude_api_key, distro)
    elif provider == "ollama":
        result = await health_mod.analyze_journal_ollama(lines, total, config.ollama_url, config.ollama_model, distro)
    else:
        return HTMLResponse('<p style="color:var(--warning);">Unknown provider.</p>', status_code=400)

    label = "Claude" if provider == "claude" else f"Ollama ({config.ollama_model})"

    # Save report
    report_path = health_mod.save_journal_report(
        config.journal_report_dir, result, label, total, distro
    )
    reports = health_mod.list_journal_reports(config.journal_report_dir)

    escaped = result.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    reports_html = _render_reports_table(reports)
    return HTMLResponse(
        f'<div class="ai-explanation" style="margin-top:12px;">'
        f'<div class="ai-label">{label} — journal analysis'
        f' <span style="font-weight:normal;color:var(--text-muted);font-size:0.85em;">'
        f'· saved as {report_path.name}</span></div>'
        f'<pre style="white-space:pre-wrap;font-family:inherit;margin:0;">{escaped}</pre>'
        f'</div>'
        f'{reports_html}'
    )


def _render_reports_table(reports: list[dict]) -> str:
    if not reports:
        return ""
    rows = ""
    for r in reports:
        rows += (
            f'<tr>'
            f'<td style="font-size:0.85em;color:var(--text-muted);white-space:nowrap;">{r["timestamp"]}</td>'
            f'<td style="font-size:0.85em;">{r["provider"]}</td>'
            f'<td><a href="#" style="font-size:0.82em;color:var(--accent);"'
            f' hx-get="/htmx/journal-report/{r["filename"]}"'
            f' hx-target="#journal-ai" hx-swap="innerHTML">View</a></td>'
            f'</tr>'
        )
    return (
        f'<div style="margin-top:14px;">'
        f'<h4 style="font-size:0.85em;color:var(--text-muted);margin-bottom:6px;">Saved reports</h4>'
        f'<table style="font-size:0.88em;width:auto;">'
        f'<thead><tr><th>Date</th><th>Provider</th><th></th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )


@router.get("/htmx/journal-report/{filename}")
async def journal_report(request: Request, filename: str):
    """HTMX: load and display a saved journal report."""
    config = request.app.state.config
    # Validate filename — only allow our own report files
    if not filename.startswith("journal-") or not filename.endswith(".md") or "/" in filename:
        return HTMLResponse('<p style="color:var(--warning);">Invalid report name.</p>', status_code=400)
    path = config.journal_report_dir / filename
    if not path.exists():
        return HTMLResponse('<p style="color:var(--warning);">Report not found.</p>', status_code=404)
    content = path.read_text()
    escaped = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    reports = health_mod.list_journal_reports(config.journal_report_dir)
    reports_html = _render_reports_table(reports)
    return HTMLResponse(
        f'<div class="ai-explanation" style="margin-top:12px;">'
        f'<div class="ai-label">Saved report — {filename}</div>'
        f'<pre style="white-space:pre-wrap;font-family:inherit;margin:0;font-size:0.88em;">{escaped}</pre>'
        f'</div>'
        f'{reports_html}'
    )
