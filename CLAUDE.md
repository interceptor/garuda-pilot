# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

### Dev Commands
```bash
poetry install
poetry run garuda-pilot
poetry run pytest
poetry run pytest tests/test_foo.py::test_bar   # single test
```

### Architecture

**Request flow:** Browser → FastAPI router (`routes/`) → DB queries + analysis modules → Jinja2 template response. HTMX partials follow the same path but return partial HTML fragments (templates named `*_content.html` or `*_table.html`).

**Startup (app.py `lifespan`):** connects DB → runs schema migrations → incrementally imports new pacman.log entries → detects hardware. The `db` and `config` objects live on `app.state` and are accessed in routes via `request.app.state.db / .config / .templates`.

**Preview refresh flow** (`routes/preview.py:_refresh_pending`): the most complex path — runs `checkupdates`, then fans out in parallel (asyncio.gather) to fetch Arch news, security advisories, and Garuda forum RSS, then scores each package and stores in `pending_updates`. HTMX POST to `/htmx/preview-refresh` triggers this and returns `preview_table.html`.

**Risk scoring** (`analysis/risk.py:score_package`): additive 0-100. Base weights by category (kernel=40, graphics=30, system=25, mesa=20, xorg=15), plus CVE severity, news mentions, Garuda news, flagged-outdated, high dep count, nvidia+kernel combo, major version bump. Trivial packages cap at 5; patch-only at 10.

**DB schema** (`db.py`): schema version 4. Migrations run in `_migrate()` — always guard new `ALTER TABLE` with try/except since SQLite doesn't support IF NOT EXISTS for columns. Key tables: `transactions` + `package_operations` (history), `pending_updates` (current check results), `transaction_logs` (warnings/scriptlet/command per txn), `security_advisories`, `news`, `garuda_news`, `health_snapshots`, `hardware_profile`, `_meta` (key/value flags like `schema_version`, `needs_log_backfill`).

**Pacman log parsing** (`pacman/log_parser.py`): incremental — tracks `log_line_end` in DB so only new lines are parsed on each startup. Regex-based line-by-line; `from_line=0` for full re-parse (used during backfill).

**Transaction type classification** (`routes/history.py:_classify_command`): inferred from the `[PACMAN] Running '...'` line captured per-transaction in `transaction_logs`.

**External data sources:**
- `checkupdates` (pacman-contrib) — available updates
- `pacman -Qi` / `pacman -Si` — local/remote package info (`pacman/query.py:bulk_query`, 2 calls total)
- `https://archlinux.org/feeds/news/` — Arch news RSS
- `https://security.archlinux.org/issues/all.json` — CVE advisories
- `https://forum.garudalinux.org/c/announcements/16.rss` — Garuda forum news
- `https://archlinux.org/packages/{repo}/{arch}/{pkg}/json/` — flag_date, deps (`analysis/pkg_api.py`)
- `garuda-health` — system health check

**System deps:** `pacman`, `checkupdates` (pacman-contrib), `lspci`, `garuda-health`

### Structure
```
garuda_pilot/
├── __main__.py          # CLI entry point
├── app.py               # FastAPI app factory + lifespan (startup logic here)
├── config.py            # TOML config (~/.config/garuda-pilot/config.toml)
├── db.py                # SQLite async wrapper, schema v4, migrations
├── models.py            # Pydantic models
├── pacman/              # checkupdates, query, log_parser, categorizer, lock
├── analysis/            # hardware, risk, news, garuda_news, security, health, pkg_api
├── routes/              # One file per page; HTMX partials inline in same file
├── templates/           # Jinja2; full pages extend base.html; partials are standalone
└── static/              # htmx.min.js, style.css (dark theme, CSS vars)
```
