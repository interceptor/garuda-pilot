# garuda-pilot

Upgrade management dashboard for Arch-based Linux distros. Runs as a local web service that provides upgrade previews with risk analysis, full searchable upgrade history, btrfs snapshots, and Arch news integration.

> **Works on any Arch-based distro** — Garuda, CachyOS, EndeavourOS, Manjaro, vanilla Arch, and more. The name is historical; the tool is not Garuda-specific.

## Features

- **Upgrade Preview** — see all pending updates before upgrading, sorted by risk score. Includes package descriptions, build dates, category badges, and expandable upstream links. Clickable CVE/Flagged/News summary badges filter the table.
- **Risk Scoring** — each pending package gets a 0-100 risk score based on category (kernel, graphics, system, mesa, xorg), CVE severity, news mentions, flagged-outdated status, hardware context (nvidia + kernel), and version bump magnitude.
- **OS-Aware Upgrade Launch** — detects your distro and available tools, recommends the right upgrade command (`garuda-update`, `eos-update`, `pamac`, `pacman`, `paru`, `yay`). Database is auto-backed up before every upgrade.
- **Btrfs Snapshots** — shows snapper history with pre/post snapshot pairs automatically created before upgrades (when snap-pac is not active). Works alongside snap-pac without duplicating snapshots.
- **Upgrade History** — every past pacman transaction is imported and searchable. Transactions are classified by type (System Upgrade, Manual Install, AUR Helper, etc.) with filter checkboxes. Navigate between transactions with prev/next buttons or arrow keys.
- **Transaction Details** — drill into any transaction to see packages, pacman command, warnings (.pacnew conflicts, DKMS errors), and scriptlet output.
- **Security Advisories** — fetches CVE data from the Arch Security Tracker. Filter by severity and status, deep-link from CVE badges in the preview.
- **Arch & Garuda News** — recent news items with automatic package name extraction. Clickable tags filter the preview table to affected packages.
- **Hardware-Aware** — detects your GPU vendor and kernel at startup, used to flag dangerous combos (e.g. nvidia module loaded + kernel update).
- **System Health** — runs `garuda-health` on Garuda, or 11 built-in parallel checks on other distros (disk space, failed services, orphan packages, pacnew files, NTP sync, journal errors, swap, core dumps, mirror freshness, pacman lock).
- **Database Backup** — schema-versioned backups with auto-pruning (keeps last 5). Backup card on dashboard, full management on about page.
- **Dark Theme** — works fully offline (vendored HTMX, no CDN).

## Distro support

| Distro | Upgrade command | Health checks | Snapshots |
|--------|----------------|---------------|-----------|
| Garuda Linux | `garuda-update` (recommended) | `garuda-health` | garuda-update manages them |
| CachyOS | `pacman` + `paru`/`yay` | Built-in (11 checks) | snapper pre/post if snap-pac absent |
| EndeavourOS | `eos-update` (recommended) | Built-in | snapper pre/post if snap-pac absent |
| Manjaro | `pamac upgrade` (recommended) | Built-in | snapper pre/post if snap-pac absent |
| Vanilla Arch | `pacman` + `paru`/`yay` | Built-in | snapper pre/post if snap-pac absent |

## Requirements

- Python >= 3.11
- `pacman` and `checkupdates` (from `pacman-contrib`)
- `lspci` (from `pciutils`, for GPU detection)
- `garuda-health` — optional, Garuda only; other distros use built-in checks
- `snapper` — optional, for btrfs snapshot history and pre-upgrade snapshots

## Installation

### Install via pipx (recommended)

```bash
sudo pacman -S python-pipx pacman-contrib pciutils
pipx install garuda-pilot
```

### Install via pip

```bash
pip install garuda-pilot
```

### Install from AUR

```bash
paru -S garuda-pilot
```

### Development install

```bash
git clone https://github.com/interceptor/garuda-pilot.git
cd garuda-pilot
poetry install
poetry run garuda-pilot
```

## Usage

```bash
garuda-pilot
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
| `/` | Dashboard — summary cards, backup status |
| `/preview` | Upgrade preview — pending updates with risk scores, filters, news warnings, upgrade launcher |
| `/history` | Transaction list — all past upgrades, filterable by type, searchable by package |
| `/history/{id}` | Transaction detail — packages, command, warnings, scriptlet output, prev/next nav |
| `/news` | Arch & Garuda news — recent items with extracted package names |
| `/security` | Security advisories — CVE data from Arch Security Tracker, filterable by severity |
| `/health` | System health — garuda-health (Garuda) or built-in checks (others), with history |
| `/snapshots` | Btrfs snapshots — snapper history, create snapshots |
| `/changelog` | Release history |
| `/about` | About page — README, database backup management |

## Configuration

Optional config file at `~/.config/garuda-pilot/config.toml`:

```toml
host = "127.0.0.1"
port = 8471
db_path = "~/.local/share/garuda-pilot/garuda-pilot.db"
pacman_log = "/var/log/pacman.log"
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
| Critical CVE | +35 |
| High CVE | +25 |
| Medium CVE | +15 |
| Mentioned in Arch news | +20 |
| Mentioned in Garuda news | +15 |
| NVIDIA module loaded + kernel update | +30 |
| Major version bump (e.g. 1.x → 2.x) | +15 |
| Flagged out-of-date on archlinux.org | +10 |
| High reverse dependency count (>10) | +10 |
| Trivial package (docs, fonts, themes) | capped at 5 |
| Patch-only update (same base version) | capped at 10 |

Risk labels: **low** (0-19), **medium** (20-39), **high** (40-59), **critical** (60-100)

## License

MIT
