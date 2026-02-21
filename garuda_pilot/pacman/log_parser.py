"""Parse /var/log/pacman.log into structured transaction data.

Supports both full parsing (initial import) and incremental parsing
(watching for new transactions from a given line number).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Regex patterns for pacman.log line types
RE_TXN_START = re.compile(
    r"^\[([^\]]+)\] \[ALPM\] transaction started$"
)
RE_TXN_END = re.compile(
    r"^\[([^\]]+)\] \[ALPM\] transaction completed$"
)
RE_UPGRADED = re.compile(
    r"^\[([^\]]+)\] \[ALPM\] upgraded ([^ ]+) \(([^ ]+) -> ([^\)]+)\)$"
)
RE_DOWNGRADED = re.compile(
    r"^\[([^\]]+)\] \[ALPM\] downgraded ([^ ]+) \(([^ ]+) -> ([^\)]+)\)$"
)
RE_INSTALLED = re.compile(
    r"^\[([^\]]+)\] \[ALPM\] installed ([^ ]+) \(([^\)]+)\)$"
)
RE_REINSTALLED = re.compile(
    r"^\[([^\]]+)\] \[ALPM\] reinstalled ([^ ]+) \(([^\)]+)\)$"
)
RE_REMOVED = re.compile(
    r"^\[([^\]]+)\] \[ALPM\] removed ([^ ]+) \(([^\)]+)\)$"
)
RE_WARNING = re.compile(
    r"^\[([^\]]+)\] \[ALPM\] warning: (.+)$"
)
RE_SCRIPTLET = re.compile(
    r"^\[([^\]]+)\] \[ALPM-SCRIPTLET\] (.+)$"
)
RE_PACMAN_CMD = re.compile(
    r"^\[([^\]]+)\] \[PACMAN\] Running '(.+)'$"
)
# Noise filter: package listings dumped by garuda-update (name version-release)
_PKG_LISTING_RE = re.compile(r"^[a-z0-9][-a-z0-9.]+ \d\S*$")


@dataclass
class ParsedOperation:
    action: str
    package_name: str
    old_version: str | None = None
    new_version: str | None = None


@dataclass
class ParsedTransaction:
    started_at: str
    completed_at: str | None = None
    log_line_start: int = 0
    log_line_end: int = 0
    operations: list[ParsedOperation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    scriptlet_output: list[str] = field(default_factory=list)
    pacman_command: str | None = None


def parse_log(log_path: Path, from_line: int = 0) -> list[ParsedTransaction]:
    """Parse pacman.log, optionally starting from a given line number.

    Args:
        log_path: Path to /var/log/pacman.log
        from_line: 0-based line number to start parsing from (for incremental)

    Returns:
        List of completed transactions found in the parsed range.
    """
    transactions: list[ParsedTransaction] = []
    current_txn: ParsedTransaction | None = None
    last_pacman_cmd: str | None = None

    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for line_num, line in enumerate(f):
            if line_num < from_line:
                continue

            line = line.rstrip("\n")

            # Track pacman commands (appear before transaction start)
            m = RE_PACMAN_CMD.match(line)
            if m:
                last_pacman_cmd = m.group(2)
                continue

            # Transaction start
            m = RE_TXN_START.match(line)
            if m:
                current_txn = ParsedTransaction(
                    started_at=m.group(1),
                    log_line_start=line_num,
                    pacman_command=last_pacman_cmd,
                )
                continue

            # Transaction end
            m = RE_TXN_END.match(line)
            if m and current_txn is not None:
                current_txn.completed_at = m.group(1)
                current_txn.log_line_end = line_num
                # Only keep transactions that have at least one operation
                if current_txn.operations:
                    transactions.append(current_txn)
                current_txn = None
                continue

            # Skip lines outside a transaction
            if current_txn is None:
                continue

            # Warnings
            m = RE_WARNING.match(line)
            if m:
                current_txn.warnings.append(m.group(2))
                continue

            # Scriptlet output
            m = RE_SCRIPTLET.match(line)
            if m:
                msg = m.group(2)
                # Filter out package listing noise (garuda-update orphan dumps)
                if not _PKG_LISTING_RE.match(msg):
                    current_txn.scriptlet_output.append(msg)
                continue

            # Upgraded
            m = RE_UPGRADED.match(line)
            if m:
                current_txn.operations.append(ParsedOperation(
                    action="upgraded",
                    package_name=m.group(2),
                    old_version=m.group(3),
                    new_version=m.group(4),
                ))
                continue

            # Downgraded
            m = RE_DOWNGRADED.match(line)
            if m:
                current_txn.operations.append(ParsedOperation(
                    action="downgraded",
                    package_name=m.group(2),
                    old_version=m.group(3),
                    new_version=m.group(4),
                ))
                continue

            # Installed
            m = RE_INSTALLED.match(line)
            if m:
                current_txn.operations.append(ParsedOperation(
                    action="installed",
                    package_name=m.group(2),
                    new_version=m.group(3),
                ))
                continue

            # Reinstalled
            m = RE_REINSTALLED.match(line)
            if m:
                current_txn.operations.append(ParsedOperation(
                    action="reinstalled",
                    package_name=m.group(2),
                    new_version=m.group(3),
                ))
                continue

            # Removed
            m = RE_REMOVED.match(line)
            if m:
                current_txn.operations.append(ParsedOperation(
                    action="removed",
                    package_name=m.group(2),
                    old_version=m.group(3),
                ))
                continue

    return transactions


def get_line_count(log_path: Path) -> int:
    """Get total line count of the log file (for incremental tracking)."""
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)
