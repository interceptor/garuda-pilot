# CLAUDE.md — Permanent Instructions

## Mandatory Behaviors

- **ALWAYS save/update project context to memory after significant work.** Memory dir: `~/.claude/projects/-home-mike-code-garuda-pilot/memory/`. If MEMORY.md is empty or stale, update it BEFORE ending a session.
- **Never re-explore the entire codebase from scratch.** Read MEMORY.md first. Only explore files relevant to the current task.
- **Don't waste tokens.** Be direct. Skip pleasantries. No over-explaining.
- **Don't create files unless asked.** Don't add docs, READMEs, comments, or type annotations unprompted.

## Project: garuda-pilot

Upgrade management dashboard for Garuda Linux / Arch-based distros.
Local web service at http://127.0.0.1:8471.

### Tech Stack
- Python 3.11+, FastAPI, Uvicorn, aiosqlite (SQLite WAL mode), Jinja2, HTMX (vendored), httpx
- Poetry for dependency management
- Entry point: `poetry run garuda-pilot`
- Fully async (subprocess, HTTP, DB)

### Structure
```
garuda_pilot/
├── __main__.py          # CLI entry point
├── app.py               # FastAPI app factory + lifespan
├── config.py            # TOML config (~/.config/garuda-pilot/config.toml)
├── db.py                # SQLite async wrapper, schema v3
├── models.py            # Pydantic models
├── pacman/              # checkupdates, query, log_parser, categorizer, lock
├── analysis/            # hardware, risk, news, garuda_news, security, health, pkg_api
├── routes/              # One file per page: dashboard, preview, history, news, health, security, changelog, about
├── templates/           # Jinja2, all extend base.html
└── static/              # htmx.min.js, style.css (dark theme)
```

### Key Details
- DB tables: transactions, package_operations, pending_updates, news, garuda_news, health_snapshots, hardware_profile, security_advisories, _meta
- Risk scoring: 0-100 per package (category, CVE severity, news mentions, flagged-outdated, deps, nvidia+kernel)
- System deps: pacman, checkupdates (pacman-contrib), lspci, garuda-health
- Routes in `routes/`, templates extend `base.html`, external data via httpx async

### Dev Commands
```bash
poetry install
poetry run garuda-pilot
poetry run pytest
```
