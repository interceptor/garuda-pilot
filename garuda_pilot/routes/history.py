"""History routes — transaction list and detail views."""

from __future__ import annotations

from fastapi import APIRouter, Request, HTTPException

from ..pacman import query

router = APIRouter()


def _short_date(pacman_date: str) -> str:
    """Shorten 'Sat 24 Jan 2026 01:47:26 CET' to '24 Jan 2026'."""
    if not pacman_date:
        return ""
    parts = pacman_date.split()
    if len(parts) >= 4:
        return f"{parts[1]} {parts[2]} {parts[3]}"
    return pacman_date


@router.get("/history")
async def history_list(request: Request):
    db = request.app.state.db
    templates = request.app.state.templates

    rows = await db.fetchall("""
        SELECT
            t.id,
            t.started_at,
            t.completed_at,
            COUNT(po.id) as total,
            SUM(CASE WHEN po.action = 'upgraded' THEN 1 ELSE 0 END) as upgraded,
            SUM(CASE WHEN po.action = 'installed' THEN 1 ELSE 0 END) as installed,
            SUM(CASE WHEN po.action = 'removed' THEN 1 ELSE 0 END) as removed,
            (SELECT COUNT(*) FROM transaction_logs tl
             WHERE tl.transaction_id = t.id AND tl.log_type = 'warning') as warning_count
        FROM transactions t
        LEFT JOIN package_operations po ON po.transaction_id = t.id
        GROUP BY t.id
        ORDER BY t.id DESC
    """)

    # Convert to dicts for template
    transactions = []
    for row in rows:
        transactions.append({
            "id": row["id"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "total": row["total"],
            "upgraded": row["upgraded"],
            "installed": row["installed"],
            "removed": row["removed"],
            "warning_count": row["warning_count"],
        })

    return templates.TemplateResponse("history.html", {
        "request": request,
        "active_page": "history",
        "transactions": transactions,
    })


@router.get("/history/{txn_id}")
async def history_detail(request: Request, txn_id: int):
    db = request.app.state.db
    templates = request.app.state.templates

    transaction = await db.fetchone(
        "SELECT * FROM transactions WHERE id = ?", (txn_id,)
    )
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")

    operations = await db.fetchall(
        """SELECT * FROM package_operations
           WHERE transaction_id = ?
           ORDER BY
               CASE action
                   WHEN 'upgraded' THEN 1
                   WHEN 'downgraded' THEN 2
                   WHEN 'installed' THEN 3
                   WHEN 'reinstalled' THEN 4
                   WHEN 'removed' THEN 5
               END,
               package_name""",
        (txn_id,),
    )

    ops = [dict(row) for row in operations]

    # Bulk-fetch descriptions and dates for all packages in this transaction
    names = [op["package_name"] for op in ops]
    info = await query.bulk_query(names)
    for op in ops:
        pi = info.get(op["package_name"])
        if pi:
            if pi.description:
                op["description"] = pi.description
            op["build_date"] = _short_date(pi.build_date)
            op["install_date"] = _short_date(pi.install_date)

    upgraded_count = sum(1 for op in ops if op["action"] == "upgraded")
    installed_count = sum(1 for op in ops if op["action"] == "installed")
    removed_count = sum(1 for op in ops if op["action"] == "removed")

    # Fetch log events (warnings, scriptlet output, pacman command)
    log_rows = await db.fetchall(
        "SELECT log_type, message FROM transaction_logs WHERE transaction_id = ? ORDER BY id",
        (txn_id,),
    )
    pacman_command = None
    warnings = []
    scriptlet_lines = []
    for row in log_rows:
        if row["log_type"] == "command":
            pacman_command = row["message"]
        elif row["log_type"] == "warning":
            warnings.append(row["message"])
        elif row["log_type"] == "scriptlet":
            scriptlet_lines.append(row["message"])

    return templates.TemplateResponse("history_detail.html", {
        "request": request,
        "active_page": "history",
        "transaction": dict(transaction),
        "operations": ops,
        "upgraded_count": upgraded_count,
        "installed_count": installed_count,
        "removed_count": removed_count,
        "pacman_command": pacman_command,
        "warnings": warnings,
        "scriptlet_lines": scriptlet_lines,
    })


@router.get("/htmx/history-search")
async def history_search(request: Request, q: str = ""):
    """HTMX partial: search transactions by package name."""
    db = request.app.state.db

    if q.strip():
        # Find transactions containing packages matching the query
        rows = await db.fetchall("""
            SELECT
                t.id,
                t.started_at,
                t.completed_at,
                COUNT(po.id) as total,
                SUM(CASE WHEN po.action = 'upgraded' THEN 1 ELSE 0 END) as upgraded,
                SUM(CASE WHEN po.action = 'installed' THEN 1 ELSE 0 END) as installed,
                SUM(CASE WHEN po.action = 'removed' THEN 1 ELSE 0 END) as removed
            FROM transactions t
            JOIN package_operations po ON po.transaction_id = t.id
            WHERE po.package_name LIKE ?
            GROUP BY t.id
            ORDER BY t.id DESC
        """, (f"%{q}%",))
    else:
        rows = await db.fetchall("""
            SELECT
                t.id,
                t.started_at,
                t.completed_at,
                COUNT(po.id) as total,
                SUM(CASE WHEN po.action = 'upgraded' THEN 1 ELSE 0 END) as upgraded,
                SUM(CASE WHEN po.action = 'installed' THEN 1 ELSE 0 END) as installed,
                SUM(CASE WHEN po.action = 'removed' THEN 1 ELSE 0 END) as removed
            FROM transactions t
            LEFT JOIN package_operations po ON po.transaction_id = t.id
            GROUP BY t.id
            ORDER BY t.id DESC
        """)

    # Return just the table body rows as HTML
    html_parts = []
    for row in rows:
        upgraded = row["upgraded"] or 0
        installed = row["installed"] or 0
        removed = row["removed"] or 0
        up_cell = f'<span style="color: var(--green);">{upgraded}</span>' if upgraded else "-"
        in_cell = f'<span style="color: var(--blue);">{installed}</span>' if installed else "-"
        rm_cell = f'<span style="color: var(--red);">{removed}</span>' if removed else "-"
        txn_id = row["id"]
        date = row["started_at"][:19].replace("T", " ")
        html_parts.append(
            f"<tr style=\"cursor: pointer;\" onclick=\"window.location='/history/{txn_id}'\">"
            f"<td>{txn_id}</td>"
            f"<td>{date}</td>"
            f'<td><strong>{row["total"]}</strong></td>'
            f"<td>{up_cell}</td>"
            f"<td>{in_cell}</td>"
            f"<td>{rm_cell}</td>"
            f"</tr>"
        )

    from fastapi.responses import HTMLResponse
    return HTMLResponse("\n".join(html_parts))
