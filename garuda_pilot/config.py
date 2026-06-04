"""Configuration management for garuda-pilot."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8471
    db_path: Path = field(default_factory=lambda: Path.home() / ".local/share/garuda-pilot/garuda-pilot.db")
    pacnew_backup_dir: Path = field(default_factory=lambda: Path.home() / ".local/share/garuda-pilot/pacnew-backups")
    journal_report_dir: Path = field(default_factory=lambda: Path.home() / ".local/share/garuda-pilot/journal-reports")
    pacman_log: Path = field(default_factory=lambda: Path("/var/log/pacman.log"))
    check_interval_minutes: int = 30
    news_interval_minutes: int = 120
    log_watch_interval_seconds: int = 10
    news_max_age_days: int = 180
    # AI providers for pacnew review
    claude_api_key: str = ""
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"

    @classmethod
    def load(cls) -> Config:
        """Load config from TOML file, falling back to defaults."""
        config_path = Path.home() / ".config/garuda-pilot/config.toml"
        if config_path.exists():
            with open(config_path, "rb") as f:
                data = tomllib.load(f)
            kwargs = {}
            for key in ("host", "port", "check_interval_minutes",
                        "news_interval_minutes", "log_watch_interval_seconds",
                        "news_max_age_days", "claude_api_key",
                        "ollama_url", "ollama_model"):
                if key in data:
                    kwargs[key] = data[key]
            for key in ("db_path", "pacman_log"):
                if key in data:
                    kwargs[key] = Path(data[key]).expanduser()
            return cls(**kwargs)
        return cls()

    def ensure_dirs(self) -> None:
        """Create data directories if they don't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.pacnew_backup_dir.mkdir(parents=True, exist_ok=True)
        self.journal_report_dir.mkdir(parents=True, exist_ok=True)

    def save(self) -> None:
        """Write current config back to config.toml, preserving unknown keys."""
        config_path = Path.home() / ".config/garuda-pilot/config.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)

        # Load existing file to preserve keys we don't manage here
        existing: dict = {}
        if config_path.exists():
            with open(config_path, "rb") as f:
                existing = tomllib.load(f)

        # Update managed keys; remove if equal to default (keep file clean)
        _defaults = Config()
        for key, value in [
            ("claude_api_key", self.claude_api_key),
            ("ollama_url", self.ollama_url),
            ("ollama_model", self.ollama_model),
        ]:
            if value and value != getattr(_defaults, key):
                existing[key] = value
            elif key in existing and not value:
                del existing[key]
            elif value == getattr(_defaults, key):
                existing.pop(key, None)

        # Serialise — all our values are strings, ints, or Paths
        lines = []
        for k, v in existing.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {str(v).lower()}")
            elif isinstance(v, int):
                lines.append(f"{k} = {v}")
            else:
                escaped = str(v).replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{k} = "{escaped}"')

        config_path.write_text("\n".join(lines) + "\n")
