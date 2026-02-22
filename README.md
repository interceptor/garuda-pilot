# garuda-pilot

Upgrade management dashboard for Garuda Linux (and other Arch-based distros). Runs as a local web service that provides upgrade previews with risk analysis, full searchable upgrade history, and Arch news integration.

## Features

- **Upgrade Preview** — see all pending updates before upgrading, sorted by risk score. Includes package descriptions, build dates, category badges, and expandable upstream links. Clickable CVE/Flagged/News summary badges filter the table.
- **Risk Scoring** — each pending package gets a 0-100 risk score based on category (kernel, graphics, system, mesa, xorg), CVE severity, news mentions, flagged-outdated status, hardware context (nvidia + kernel), and version bump magnitude.
- **Upgrade Launch** — launch `garuda-update` or `sudo pacman -Syu` directly in your terminal emulator (konsole, kitty, alacritty, xterm). Database is auto-backed up before every upgrade.
- **Upgrade History** — every past pacman transaction is imported and searchable. Transactions are classified by type (System Upgrade, Manual Install, AUR Helper, etc.) with filter checkboxes. Navigate between transactions with prev/next buttons or arrow keys.
- **Transaction Details** — drill into any transaction to see packages, pacman command, warnings (.pacnew conflicts, DKMS errors), and scriptlet output.
- **Security Advisories** — fetches CVE data from the Arch Security Tracker. Filter by severity and status, deep-link from CVE badges in the preview.
- **Arch & Garuda News** — recent news items with automatic package name extraction. Clickable tags filter the preview table to affected packages.
- **Hardware-Aware** — detects your GPU vendor and kernel at startup, used to flag dangerous combos (e.g. nvidia module loaded + kernel update).
- **System Health** — wraps `garuda-health` to run 25+ system checks (orphan packages, failed services, pacnew files, disk space, etc.) with severity levels and fix suggestions. Stores historical snapshots.
- **Database Backup** — schema-versioned backups with auto-pruning (keeps last 5). Backup card on dashboard, full management on about page. Schema version validated on restore.
- **Filters** — hide trivial packages (docs, fonts, themes), hide patch-only updates, filter by category, search by name or description. All tables show row counts.
- **Dark Theme** — Garuda-style dark UI, works offline (vendored HTMX, no CDN).

## Requirements

- Python >= 3.11
- [Poetry](https://python-poetry.org/) (dependency management)
- `pacman` and `checkupdates` (from `pacman-contrib`)
- `lspci` (from `pciutils`, for GPU detection)
- `garuda-health` (for system health checks — pre-installed on Garuda)
- An Arch-based system with `/var/log/pacman.log`

On Garuda/Arch, install prerequisites:

```bash
sudo pacman -S python poetry pacman-contrib pciutils
```

## Installation

```bash
git clone <repo-url> garuda-pilot
cd garuda-pilot
poetry install
```

## Usage

### Run directly

```bash
poetry run garuda-pilot
```

Or equivalently:

```bash
poetry run python -m garuda_pilot
```

The dashboard is available at **http://127.0.0.1:8471**

### First run

On first startup, garuda-pilot will:

1. Create its database at `~/.local/share/garuda-pilot/garuda-pilot.db`
2. Import your full pacman.log history (all past transactions)
3. Detect your GPU and kernel

Navigate to `/preview` to run `checkupdates` and see pending updates with risk scores.

### Pages

| URL | Description |
|-----|-------------|
| `/` | Dashboard — summary cards, backup status, upgrade button |
| `/preview` | Upgrade preview — pending updates with risk scores, filters, news warnings |
| `/history` | Transaction list — all past upgrades, filterable by type, searchable by package |
| `/history/{id}` | Transaction detail — packages, command, warnings, scriptlet output, prev/next nav |
| `/news` | Arch & Garuda news — recent items with extracted package names |
| `/security` | Security advisories — CVE data from Arch Security Tracker, filterable by severity |
| `/health` | System health — garuda-health results with severity breakdown and history |
| `/about` | About page — README, database backup management |

### Refresh data

- **Preview**: click the **Refresh** button to re-run `checkupdates` and update risk scores
- **News**: click **Refresh** on the news page, or news is auto-fetched when stale (>2 hours)
- **Health**: click **Run Health Check** to run garuda-health and store a snapshot
- **History**: new transactions are imported from pacman.log on startup (if DB was empty)

## Configuration

Optional config file at `~/.config/garuda-pilot/config.toml`:

```toml
host = "127.0.0.1"
port = 8471
db_path = "~/.local/share/garuda-pilot/garuda-pilot.db"
pacman_log = "/var/log/pacman.log"
check_interval_minutes = 30
news_interval_minutes = 120
```

All fields are optional — defaults are shown above.

## Risk Scoring

Each pending package is scored 0-100:

| Factor | Points |
|--------|--------|
| Category: kernel | +40 |
| Category: graphics (nvidia, vulkan, etc.) | +30 |
| Category: system (systemd, glibc, etc.) | +25 |
| Category: mesa | +20 |
| Category: xorg/wayland | +15 |
| Mentioned in Arch news | +20 |
| NVIDIA module loaded + kernel update | +30 |
| Major version bump (e.g. 1.x -> 2.x) | +15 |
| Trivial package (docs, fonts, themes) | capped at 5 |
| Patch-only update (same base version) | capped at 10 |

Risk labels: **low** (0-19), **medium** (20-39), **high** (40-59), **critical** (60-100)

## Project Structure

```
garuda-pilot/
├── pyproject.toml
├── garuda_pilot/
│   ├── __main__.py          # Entry point
│   ├── app.py               # FastAPI app factory + lifespan
│   ├── config.py            # TOML config loading
│   ├── db.py                # SQLite schema + async wrapper
│   ├── models.py            # Pydantic models
│   ├── pacman/
│   │   ├── checkupdates.py  # Async checkupdates wrapper
│   │   ├── query.py         # Bulk pacman -Qi/-Si queries
│   │   ├── log_parser.py    # Pacman.log parser
│   │   ├── categorizer.py   # Package categorization
│   │   └── lock.py          # Pacman DB lock detection
│   ├── analysis/
│   │   ├── news.py          # Arch RSS fetch + package extraction
│   │   ├── garuda_news.py   # Garuda forum RSS
│   │   ├── security.py      # Arch Security Tracker
│   │   ├── hardware.py      # GPU/kernel detection
│   │   ├── risk.py          # Risk scoring engine
│   │   ├── health.py        # garuda-health wrapper
│   │   └── pkg_api.py       # Arch package API (flagged/deps)
│   ├── routes/
│   │   ├── dashboard.py     # GET /
│   │   ├── preview.py       # GET /preview + upgrade launch
│   │   ├── history.py       # GET /history + detail + type filters
│   │   ├── news.py          # GET /news + HTMX refresh
│   │   ├── security.py      # GET /security + CVE filters
│   │   ├── health.py        # GET /health + HTMX refresh
│   │   ├── about.py         # GET /about + backup/restore
│   │   └── changelog.py     # GET /changelog
│   ├── templates/           # Jinja2 HTML templates
│   └── static/              # CSS + vendored HTMX
└── tests/
```

## Development

```bash
poetry install
poetry run garuda-pilot        # run the server
poetry run pytest              # run tests
```

## License

MIT
