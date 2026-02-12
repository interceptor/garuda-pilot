"""Wrapper around garuda-health for system health checks.

Runs garuda-health (text output), strips ANSI codes, parses check counts,
severity headers, and issue descriptions.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

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

# Known garuda-health check names (from garuda-health source)
CHECK_NAMES = [
    "Orphan packages",
    "Failed systemd services",
    "Pacnew files",
    "Pacsave files",
    "Disk space (root)",
    "Disk space (home)",
    "Disk space (boot)",
    "Broken symlinks",
    "Journal errors (24h)",
    "Core dumps",
    "Missing firmware",
    "Keyring status",
    "Mirror freshness",
    "Package database lock",
    "Duplicate packages",
    "Foreign packages",
    "Missing dependencies",
    "Unneeded packages",
    "Cache size",
    "Kernel modules",
    "System time sync",
    "Swap usage",
    "Memory pressure",
    "CPU temperature",
    "SMART disk health",
    "Network connectivity",
]


@dataclass
class HealthIssue:
    """A single health check issue."""
    severity: str
    description: str
    fix_available: bool = False


@dataclass
class HealthResult:
    """Parsed result of a garuda-health run."""
    issues: list[HealthIssue] = field(default_factory=list)
    all_clear: bool = True
    checks_run: int = 0
    total_checks: int = 0
    duration_seconds: float = 0.0
    error: str = ""

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
        """Serialize for DB storage."""
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
        """Deserialize from DB storage."""
        data = json.loads(raw)
        issues = [
            HealthIssue(**i) for i in data.get("issues", [])
        ]
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
    """Comparison between current and previous health snapshots."""
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


def _parse_text_output(text: str) -> HealthResult:
    """Parse garuda-health text output into a HealthResult."""
    # Strip ANSI codes
    clean = _ANSI_RE.sub("", text)

    checks_run = 0
    total_checks = 0
    duration = 0.0
    current_severity = "INFO"
    issues: list[HealthIssue] = []

    for line in clean.splitlines():
        line = line.rstrip()

        # Check for summary line
        m = _CHECKS_RE.search(line)
        if m:
            checks_run = int(m.group(1))
            total_checks = int(m.group(2))
            duration = float(m.group(3))
            continue

        # Check for severity header
        m = _SEV_HEADER_RE.search(line)
        if m:
            current_severity = m.group(1).upper()
            continue

        # Check for issue line
        m = _ISSUE_RE.match(line)
        if m:
            desc = m.group(1).strip()
            if desc:
                # Detect if fix is mentioned
                fix_available = "(fix available)" in desc.lower() or "(fixable)" in desc.lower()
                desc = re.sub(r"\s*\(fix(?:able| available)\)", "", desc, flags=re.IGNORECASE).strip()
                issues.append(HealthIssue(
                    severity=current_severity,
                    description=desc,
                    fix_available=fix_available,
                ))

    return HealthResult(
        issues=issues,
        all_clear=len(issues) == 0,
        checks_run=checks_run,
        total_checks=total_checks,
        duration_seconds=duration,
    )


async def run_health_check() -> HealthResult:
    """Run garuda-health and parse results.

    Tries text output first (for check counts), falls back to --json.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "garuda-health",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except FileNotFoundError:
        return HealthResult(error="garuda-health not found")
    except asyncio.TimeoutError:
        return HealthResult(error="garuda-health timed out (60s)")

    raw = stdout.decode(errors="replace").strip()
    if not raw:
        err = stderr.decode(errors="replace").strip()
        if err:
            return HealthResult(error=f"garuda-health error: {err}")
        return HealthResult(error="garuda-health produced no output")

    result = _parse_text_output(raw)

    # If text parsing didn't find check counts, try --json fallback
    if result.checks_run == 0 and not result.issues:
        return await _run_json_fallback(result)

    return result


async def _run_json_fallback(text_result: HealthResult) -> HealthResult:
    """Fallback: run garuda-health --json for structured data."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "garuda-health", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except (FileNotFoundError, asyncio.TimeoutError):
        return text_result  # Return whatever text parsing got

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

    return HealthResult(
        issues=issues,
        all_clear=len(issues) == 0,
        checks_run=text_result.checks_run,
        total_checks=text_result.total_checks,
        duration_seconds=text_result.duration_seconds,
    )


async def store_snapshot(db, result: HealthResult, source: str = "manual") -> None:
    """Store a health check result as a snapshot in the database."""
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO health_snapshots (checked_at, source, results) VALUES (?, ?, ?)",
        (now, source, result.to_json()),
    )
    await db.commit()


async def load_latest_snapshot(db) -> tuple[HealthResult | None, str | None]:
    """Load the most recent health snapshot. Returns (result, checked_at) or (None, None)."""
    row = await db.fetchone(
        "SELECT results, checked_at FROM health_snapshots ORDER BY id DESC LIMIT 1"
    )
    if not row:
        return None, None
    return HealthResult.from_json(row["results"]), row["checked_at"]


async def load_snapshot_history(db, limit: int = 20) -> list[dict]:
    """Load recent health snapshots for the history table."""
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
    """Compare current health result with the previous snapshot.

    Returns None if there's no previous snapshot to compare against.
    """
    rows = await db.fetchall(
        "SELECT results FROM health_snapshots ORDER BY id DESC LIMIT 2"
    )

    # Need at least 2 snapshots (current was already stored, plus one previous)
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
