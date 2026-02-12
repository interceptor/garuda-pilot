"""Pydantic models shared across API and templates."""

from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel


class PackageOperation(BaseModel):
    id: int | None = None
    transaction_id: int
    action: str  # 'upgraded', 'installed', 'removed', 'downgraded', 'reinstalled'
    package_name: str
    old_version: str | None = None
    new_version: str | None = None
    description: str | None = None
    category: str | None = None
    is_trivial: bool = False
    is_patch: bool = False
    risk_flags: list[str] | None = None


class Transaction(BaseModel):
    id: int | None = None
    started_at: str
    completed_at: str | None = None
    source: str = "log"
    log_line_start: int | None = None
    log_line_end: int | None = None
    operations: list[PackageOperation] = []

    @property
    def upgraded_count(self) -> int:
        return sum(1 for op in self.operations if op.action == "upgraded")

    @property
    def installed_count(self) -> int:
        return sum(1 for op in self.operations if op.action == "installed")

    @property
    def removed_count(self) -> int:
        return sum(1 for op in self.operations if op.action == "removed")

    @property
    def total_count(self) -> int:
        return len(self.operations)


class PendingUpdate(BaseModel):
    package_name: str
    old_version: str
    new_version: str
    description: str | None = None
    url: str | None = None
    old_date: str | None = None
    new_date: str | None = None
    category: str | None = None
    is_trivial: bool = False
    is_patch: bool = False
    in_news: bool = False
    risk_score: int = 0
    risk_flags: list[str] | None = None


class NewsItem(BaseModel):
    guid: str
    title: str
    link: str
    published_at: str
    mentioned_packages: list[str] = []


class HealthSnapshot(BaseModel):
    id: int | None = None
    checked_at: str
    source: str = "auto"
    results: dict | list = {}


class HardwareProfile(BaseModel):
    gpu_vendor: str | None = None
    gpu_model: str | None = None
    kernel: str | None = None
    kernel_version: str | None = None
    nvidia_module_loaded: bool = False
