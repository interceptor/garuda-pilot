"""System health checks — garuda-health on Garuda, native checks elsewhere."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Severity ordering (worst first)
SEVERITIES = ("CRITICAL", "HIGH", "LOW", "INFO")

# Strip ANSI escape codes
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# Match "X/Y checks run in Z seconds"
_CHECKS_RE = re.compile(r"(\d+)/(\d+)\s+checks?\s+run\s+in\s+([\d.]+)\s*s", re.IGNORECASE)
# Match severity headers like "--- CRITICAL ---" or "=== HIGH ==="
_SEV_HEADER_RE = re.compile(r"[-=]{3,}\s*(CRITICAL|HIGH|LOW|INFO)\s*[-=]{3,}", re.IGNORECASE)
# Match issue lines like " - Some issue description"
_ISSUE_RE = re.compile(r"^\s*[-*]\s+(.+)$")

# Check names for the all-clear grid view
CHECK_NAMES = [
    "Orphan packages", "Failed systemd services", "Pacnew files", "Pacsave files",
    "Disk space (root)", "Disk space (home)", "Disk space (boot)", "Broken symlinks",
    "Journal errors (24h)", "Core dumps", "Missing firmware", "Keyring status",
    "Mirror freshness", "Package database lock", "Duplicate packages", "Foreign packages",
    "Missing dependencies", "Unneeded packages", "Cache size", "Kernel modules",
    "System time sync", "Swap usage", "Memory pressure", "CPU temperature",
    "SMART disk health", "Network connectivity",
]

NATIVE_CHECK_NAMES = [
    "Disk space (/)", "Disk space (/home)", "Disk space (/boot)",
    "Failed systemd services", "Orphan packages", "Pacnew files", "Pacsave files",
    "Pacman database lock", "NTP time sync", "Journal errors (24h)",
    "Swap usage", "Core dumps", "Mirror freshness",
]


@dataclass
class HealthIssue:
    severity: str
    description: str
    fix_available: bool = False


@dataclass
class HealthResult:
    issues: list[HealthIssue] = field(default_factory=list)
    all_clear: bool = True
    checks_run: int = 0
    total_checks: int = 0
    duration_seconds: float = 0.0
    error: str = ""
    backend: str = ""  # "garuda-health" or "native" — not persisted

    @property
    def issue_count(self) -> int:
        return len(self.issues)

    @property
    def checks_passed(self) -> int:
        return self.checks_run - self.issue_count

    @property
    def worst_severity(self) -> str:
        if not self.issues:
            return "OK"
        for sev in SEVERITIES:
            if any(i.severity == sev for i in self.issues):
                return sev
        return "OK"

    def count_by_severity(self, severity: str) -> int:
        return sum(1 for i in self.issues if i.severity == severity)

    @property
    def has_fixable(self) -> bool:
        return any(i.fix_available for i in self.issues)

    def to_json(self) -> str:
        return json.dumps({
            "issues": [
                {"severity": i.severity, "description": i.description,
                 "fix_available": i.fix_available}
                for i in self.issues
            ],
            "all_clear": self.all_clear,
            "checks_run": self.checks_run,
            "total_checks": self.total_checks,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
        })

    @classmethod
    def from_json(cls, raw: str) -> HealthResult:
        data = json.loads(raw)
        issues = [HealthIssue(**i) for i in data.get("issues", [])]
        return cls(
            issues=issues,
            all_clear=data.get("all_clear", not issues),
            checks_run=data.get("checks_run", 0),
            total_checks=data.get("total_checks", 0),
            duration_seconds=data.get("duration_seconds", 0.0),
            error=data.get("error", ""),
        )


@dataclass
class HealthComparison:
    new_issues: list[str] = field(default_factory=list)
    resolved_issues: list[str] = field(default_factory=list)
    unchanged_issues: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.new_issues or self.resolved_issues)

    @property
    def summary(self) -> str:
        parts = []
        if self.new_issues:
            parts.append(f"{len(self.new_issues)} new")
        if self.resolved_issues:
            parts.append(f"{len(self.resolved_issues)} resolved")
        if self.unchanged_issues:
            parts.append(f"{len(self.unchanged_issues)} unchanged")
        return ", ".join(parts) if parts else "No changes"


# ---------------------------------------------------------------------------
# garuda-health backend
# ---------------------------------------------------------------------------

def _parse_text_output(text: str) -> HealthResult:
    clean = _ANSI_RE.sub("", text)
    checks_run = total_checks = 0
    duration = 0.0
    current_severity = "INFO"
    issues: list[HealthIssue] = []

    for line in clean.splitlines():
        line = line.rstrip()
        m = _CHECKS_RE.search(line)
        if m:
            checks_run = int(m.group(1))
            total_checks = int(m.group(2))
            duration = float(m.group(3))
            continue
        m = _SEV_HEADER_RE.search(line)
        if m:
            current_severity = m.group(1).upper()
            continue
        m = _ISSUE_RE.match(line)
        if m:
            desc = m.group(1).strip()
            if desc:
                fix_available = "(fix available)" in desc.lower() or "(fixable)" in desc.lower()
                desc = re.sub(r"\s*\(fix(?:able| available)\)", "", desc, flags=re.IGNORECASE).strip()
                issues.append(HealthIssue(severity=current_severity, description=desc,
                                          fix_available=fix_available))

    return HealthResult(issues=issues, all_clear=len(issues) == 0,
                        checks_run=checks_run, total_checks=total_checks,
                        duration_seconds=duration)


async def _run_json_fallback(text_result: HealthResult) -> HealthResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            "garuda-health", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except (FileNotFoundError, asyncio.TimeoutError):
        return text_result
    raw = stdout.decode(errors="replace").strip()
    if not raw:
        return text_result
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return text_result
    if not data:
        return HealthResult(all_clear=True)
    issues: list[HealthIssue] = []
    for severity in SEVERITIES:
        for item in data.get(severity, []):
            issues.append(HealthIssue(
                severity=severity,
                description=item.get("description", "Unknown issue"),
                fix_available=item.get("fix_available", False),
            ))
    return HealthResult(issues=issues, all_clear=len(issues) == 0,
                        checks_run=text_result.checks_run,
                        total_checks=text_result.total_checks,
                        duration_seconds=text_result.duration_seconds)


async def _run_garuda_health() -> HealthResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            "garuda-health",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        return HealthResult(error="garuda-health timed out (60s)", backend="garuda-health")

    raw = stdout.decode(errors="replace").strip()
    if not raw:
        err = stderr.decode(errors="replace").strip()
        return HealthResult(error=f"garuda-health error: {err}" if err else "garuda-health produced no output",
                            backend="garuda-health")

    result = _parse_text_output(raw)
    if result.checks_run == 0 and not result.issues:
        result = await _run_json_fallback(result)

    result.backend = "garuda-health"
    return result


# ---------------------------------------------------------------------------
# Native backend — standard Arch/systemd tools
# ---------------------------------------------------------------------------

async def _check_disk_space() -> list[HealthIssue]:
    issues = []
    seen: set[int] = set()
    for mount in ('/', '/home', '/boot'):
        try:
            dev = os.stat(mount).st_dev
            if dev in seen:
                continue
            seen.add(dev)
            usage = shutil.disk_usage(mount)
            pct = usage.used / usage.total * 100
            free_gb = usage.free / (1024 ** 3)
            if pct >= 95:
                issues.append(HealthIssue("CRITICAL",
                    f"Disk {mount} is {pct:.0f}% full ({free_gb:.1f} GB free)"))
            elif pct >= 85:
                issues.append(HealthIssue("HIGH",
                    f"Disk {mount} is {pct:.0f}% full ({free_gb:.1f} GB free)"))
        except OSError:
            pass
    return issues


async def _check_failed_services() -> list[HealthIssue]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--failed", "--no-legend", "--no-pager",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (FileNotFoundError, asyncio.TimeoutError):
        return []
    issues = []
    for line in stdout.decode(errors="replace").splitlines():
        line = line.strip().lstrip("● ")
        if not line or "units listed" in line:
            continue
        parts = line.split()
        if parts and "failed" in line.lower():
            issues.append(HealthIssue("HIGH", f"Failed service: {parts[0]}"))
    return issues


async def _check_orphan_packages() -> list[HealthIssue]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "pacman", "-Qdtq",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except (FileNotFoundError, asyncio.TimeoutError):
        return []
    orphans = [p for p in stdout.decode(errors="replace").splitlines() if p.strip()]
    if not orphans:
        return []
    sample = ", ".join(orphans[:5]) + ("..." if len(orphans) > 5 else "")
    return [HealthIssue("LOW", f"{len(orphans)} orphan package(s): {sample}")]


async def _check_pacnew_files() -> list[HealthIssue]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "find", "/etc", "-name", "*.pacnew", "-type", "f",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except (FileNotFoundError, asyncio.TimeoutError):
        return []
    files = [f for f in stdout.decode(errors="replace").splitlines() if f.strip()]
    if not files:
        return []
    sample = ", ".join(files[:3]) + ("..." if len(files) > 3 else "")
    return [HealthIssue("LOW", f"{len(files)} pacnew file(s) need review: {sample}")]


async def _check_pacsave_files() -> list[HealthIssue]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "find", "/etc", "-name", "*.pacsave", "-type", "f",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except (FileNotFoundError, asyncio.TimeoutError):
        return []
    files = [f for f in stdout.decode(errors="replace").splitlines() if f.strip()]
    if not files:
        return []
    return [HealthIssue("INFO", f"{len(files)} pacsave file(s) left from removed packages")]


async def _check_pacman_lock() -> list[HealthIssue]:
    if Path("/var/lib/pacman/db.lck").exists():
        return [HealthIssue("CRITICAL",
            "Pacman database is locked — remove /var/lib/pacman/db.lck if no pacman process is running")]
    return []


async def _check_time_sync() -> list[HealthIssue]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "timedatectl", "show",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except (FileNotFoundError, asyncio.TimeoutError):
        return []
    output = stdout.decode(errors="replace")
    if "NTPSynchronized=no" in output:
        return [HealthIssue("HIGH", "System time is not NTP-synchronized")]
    return []


async def _check_journal_errors() -> list[HealthIssue]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "journalctl", "-p", "err", "--since", "24h ago",
            "--no-pager", "-q", "--output=short",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except (FileNotFoundError, asyncio.TimeoutError):
        return []
    count = sum(1 for l in stdout.decode(errors="replace").splitlines() if l.strip())
    if count >= 50:
        return [HealthIssue("HIGH", f"{count} journal errors in the past 24h")]
    if count >= 10:
        return [HealthIssue("LOW", f"{count} journal errors in the past 24h")]
    return []


async def _check_swap_usage() -> list[HealthIssue]:
    try:
        meminfo = Path("/proc/meminfo").read_text()
    except OSError:
        return []
    swap_total = swap_free = 0
    for line in meminfo.splitlines():
        if line.startswith("SwapTotal:"):
            swap_total = int(line.split()[1])
        elif line.startswith("SwapFree:"):
            swap_free = int(line.split()[1])
    if swap_total == 0:
        return []
    pct = (swap_total - swap_free) / swap_total * 100
    if pct >= 90:
        return [HealthIssue("HIGH", f"Swap is {pct:.0f}% full ({swap_total // 1024} MB total)")]
    if pct >= 70:
        return [HealthIssue("LOW", f"Swap is {pct:.0f}% used")]
    return []


async def _check_core_dumps() -> list[HealthIssue]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "coredumpctl", "list", "--no-pager", "-q",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (FileNotFoundError, asyncio.TimeoutError):
        return []
    lines = [l for l in stdout.decode(errors="replace").splitlines() if l.strip()]
    if lines:
        return [HealthIssue("LOW", f"{len(lines)} core dump(s) found — run 'coredumpctl list'")]
    return []


async def _check_mirror_freshness() -> list[HealthIssue]:
    sync_dir = Path("/var/lib/pacman/sync")
    if not sync_dir.exists():
        return []
    db_files = list(sync_dir.glob("*.db"))
    if not db_files:
        return []
    oldest_mtime = min(p.stat().st_mtime for p in db_files)
    age_hours = (datetime.now().timestamp() - oldest_mtime) / 3600
    if age_hours > 168:
        return [HealthIssue("LOW",
            f"Package databases are {age_hours / 24:.0f} days old — run 'sudo pacman -Sy'")]
    return []


async def _run_native_checks() -> HealthResult:
    import time
    start = time.monotonic()

    check_fns = [
        _check_disk_space,
        _check_failed_services,
        _check_orphan_packages,
        _check_pacnew_files,
        _check_pacsave_files,
        _check_pacman_lock,
        _check_time_sync,
        _check_journal_errors,
        _check_swap_usage,
        _check_core_dumps,
        _check_mirror_freshness,
    ]

    results = await asyncio.gather(*[fn() for fn in check_fns], return_exceptions=True)

    issues: list[HealthIssue] = []
    checks_run = 0
    for r in results:
        checks_run += 1
        if isinstance(r, list):
            issues.extend(r)

    duration = round(time.monotonic() - start, 2)
    return HealthResult(
        issues=issues,
        all_clear=len(issues) == 0,
        checks_run=checks_run,
        total_checks=checks_run,
        duration_seconds=duration,
        backend="native",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Journal error fetching and AI analysis
# ---------------------------------------------------------------------------

async def fetch_journal_errors(display_limit: int = 200) -> tuple[list[str], int]:
    """Fetch error-level journal entries from the past 24h.

    Returns (lines[:display_limit], total_count).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "journalctl", "-p", "err", "--since", "24h ago",
            "--no-pager", "-q", "--output=short",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except (FileNotFoundError, asyncio.TimeoutError):
        return [], 0
    lines = [l for l in stdout.decode(errors="replace").splitlines() if l.strip()]
    return lines[:display_limit], len(lines)


def _journal_ai_prompt(lines: list[str], total: int, distro: str) -> str:
    shown = lines[:100]
    sample_text = "\n".join(shown)
    truncation = f"\n... ({total - len(shown)} more lines not shown)" if total > len(shown) else ""
    return f"""\
You are a Linux system administrator analyzing journal error logs.
System: {distro or "Arch-based Linux"}
Total errors in past 24h: {total}

Journal errors (up to 100 lines shown):
{sample_text}{truncation}

Respond in plain text, no markdown. Use this EXACT format:

SUMMARY:
[2-3 sentences: what is happening overall]

CATEGORIES:
- [service or source]: [what these errors mean] (N occurrences)

FIXES:
1. [Most important fix — include the exact command if applicable]
2. [Next fix]
3. [etc.]

Focus only on actionable issues. Skip routine/benign boot messages.
"""


async def analyze_journal_claude(lines: list[str], total: int, api_key: str, distro: str = "") -> str:
    import httpx
    prompt = _journal_ai_prompt(lines, total, distro)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1024,
                      "messages": [{"role": "user", "content": prompt}]},
            )
    except Exception as e:
        return f"Network error: {e}"
    if resp.status_code == 401:
        return "Invalid Claude API key — check Settings."
    if resp.status_code != 200:
        try:
            msg = resp.json().get("error", {}).get("message", resp.text[:200])
        except Exception:
            msg = resp.text[:200]
        return f"Claude API HTTP {resp.status_code}: {msg}"
    try:
        return resp.json()["content"][0]["text"].strip()
    except (KeyError, IndexError):
        return "Unexpected response from Claude."


async def analyze_journal_ollama(lines: list[str], total: int, base_url: str, model: str, distro: str = "") -> str:
    import httpx
    prompt = _journal_ai_prompt(lines, total, distro)
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/api/chat",
                json={"model": model,
                      "messages": [{"role": "user", "content": prompt}],
                      "stream": False},
            )
    except httpx.ConnectError:
        return f"Cannot connect to Ollama at {base_url} — is it running?"
    except httpx.TimeoutException:
        return "Ollama timed out. Try again or use Claude."
    except Exception as e:
        return f"Network error: {e}"
    if resp.status_code == 404:
        return f"Ollama model '{model}' not found — run: ollama pull {model}"
    if resp.status_code != 200:
        return f"Ollama HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        return resp.json()["message"]["content"].strip()
    except (KeyError, TypeError):
        return "Unexpected response from Ollama."


def save_journal_report(report_dir: Path, analysis: str, provider: str,
                        total_errors: int, distro: str) -> Path:
    """Save an AI journal analysis as a markdown file. Returns the saved path."""
    from datetime import datetime
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now()
    ts_str = ts.strftime("%Y%m%d-%H%M%S")
    safe_provider = provider.replace(" ", "_").replace("(", "").replace(")", "").replace(":", "")
    filename = f"journal-{ts_str}-{safe_provider}.md"
    content = (
        f"# Journal Error Analysis\n\n"
        f"**Date:** {ts.strftime('%Y-%m-%d %H:%M:%S')}  \n"
        f"**Provider:** {provider}  \n"
        f"**System:** {distro or 'Arch-based Linux'}  \n"
        f"**Total errors (24h):** {total_errors}  \n\n"
        f"---\n\n"
        f"{analysis}\n"
    )
    path = report_dir / filename
    path.write_text(content)
    return path


def list_journal_reports(report_dir: Path) -> list[dict]:
    """Return saved journal reports sorted newest first."""
    if not report_dir.exists():
        return []
    reports = []
    for p in sorted(report_dir.glob("journal-*.md"), reverse=True):
        name = p.stem  # journal-20260605-103000-claude
        parts = name.split("-", 3)  # ['journal', 'YYYYMMDD', 'HHMMSS', 'provider...']
        ts_display = ""
        provider = ""
        if len(parts) >= 3:
            d, t = parts[1], parts[2]
            ts_display = f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}:{t[4:]}"
            provider = parts[3].replace("_", " ") if len(parts) > 3 else ""
        reports.append({
            "filename": p.name,
            "timestamp": ts_display,
            "provider": provider,
            "size": p.stat().st_size,
        })
    return reports


def _read_distro() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


async def run_health_check() -> HealthResult:
    """Run health check using garuda-health if available, native checks otherwise."""
    if shutil.which("garuda-health"):
        return await _run_garuda_health()
    return await _run_native_checks()


async def store_snapshot(db, result: HealthResult, source: str = "manual") -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO health_snapshots (checked_at, source, results) VALUES (?, ?, ?)",
        (now, source, result.to_json()),
    )
    await db.commit()


async def load_latest_snapshot(db) -> tuple[HealthResult | None, str | None]:
    row = await db.fetchone(
        "SELECT results, checked_at FROM health_snapshots ORDER BY id DESC LIMIT 1"
    )
    if not row:
        return None, None
    return HealthResult.from_json(row["results"]), row["checked_at"]


async def load_snapshot_history(db, limit: int = 20) -> list[dict]:
    rows = await db.fetchall(
        "SELECT id, checked_at, source, results FROM health_snapshots ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    snapshots = []
    for row in rows:
        result = HealthResult.from_json(row["results"])
        snapshots.append({
            "id": row["id"],
            "checked_at": row["checked_at"],
            "source": row["source"],
            "issue_count": result.issue_count,
            "worst_severity": result.worst_severity,
            "all_clear": result.all_clear,
            "error": result.error,
        })
    return snapshots


async def compare_with_previous(db, current: HealthResult) -> HealthComparison | None:
    rows = await db.fetchall(
        "SELECT results FROM health_snapshots ORDER BY id DESC LIMIT 2"
    )
    if len(rows) < 2:
        return None
    previous = HealthResult.from_json(rows[1]["results"])
    current_descs = {i.description for i in current.issues}
    previous_descs = {i.description for i in previous.issues}
    return HealthComparison(
        new_issues=sorted(current_descs - previous_descs),
        resolved_issues=sorted(previous_descs - current_descs),
        unchanged_issues=sorted(current_descs & previous_descs),
    )
