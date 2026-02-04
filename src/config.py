"""Configuration management for the podcast automation system."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    # Database
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/podcast.db",
        alias="DATABASE_URL",
    )

    # OpenAI
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_api_base: str = Field(
        default="https://api.openai.com/v1", alias="OPENAI_API_BASE"
    )
    openai_model: str = Field(default="gpt-4", alias="OPENAI_MODEL")
    whisper_model: str = Field(default="whisper-1", alias="WHISPER_MODEL")

    # iTunes
    itunes_api_enabled: bool = Field(default=True, alias="ITUNES_API_ENABLED")

    # Spotify
    spotify_client_id: str = Field(default="", alias="SPOTIFY_CLIENT_ID")
    spotify_client_secret: str = Field(default="", alias="SPOTIFY_CLIENT_SECRET")

    # Scheduler
    scheduler_cron_hour: int = Field(default=23, alias="SCHEDULER_CRON_HOUR")
    scheduler_cron_minute: int = Field(default=0, alias="SCHEDULER_CRON_MINUTE")
    scheduler_timezone: str = Field(default="Asia/Shanghai", alias="SCHEDULER_TIMEZONE")

    # Processing
    max_concurrent_feeds: int = Field(default=5, alias="MAX_CONCURRENT_FEEDS")
    incremental_update_hours: int = Field(default=24, alias="INCREMENTAL_UPDATE_HOURS")
    max_retry_attempts: int = Field(default=3, alias="MAX_RETRY_ATTEMPTS")
    retry_backoff_base: int = Field(default=2, alias="RETRY_BACKOFF_BASE")

    # Storage
    summaries_dir: str = Field(default="./summaries", alias="SUMMARIES_DIR")
    audio_temp_dir: str = Field(default="./temp/audio", alias="AUDIO_TEMP_DIR")
    log_dir: str = Field(default="./logs", alias="LOG_DIR")

    # Server
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    debug: bool = Field(default=False, alias="DEBUG")

    # Memory monitoring
    memory_limit_mb: int = Field(default=1024, alias="MEMORY_LIMIT_MB")
    memory_check_interval: int = Field(default=60, alias="MEMORY_CHECK_INTERVAL")

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }


class YAMLConfig:
    """YAML configuration loader for podcast list and detailed settings."""

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or os.getenv(
            "CONFIG_PATH", "config/config.yaml"
        )
        self._data: dict = {}
        self._load()

    def _load(self):
        path = Path(self.config_path)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                self._data = yaml.safe_load(f) or {}

    def get(self, key: str, default=None):
        keys = key.split(".")
        value = self._data
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default
        return value

    @property
    def podcasts(self) -> list[dict]:
        return self._data.get("podcasts", [])

    @property
    def analysis_config(self) -> dict:
        return self.get("processing.analysis", {})

    @property
    def scheduler_config(self) -> dict:
        return self.get("scheduler", {})

    @property
    def audio_config(self) -> dict:
        return self.get("processing.audio", {})

    def reload(self):
        self._load()


def get_settings() -> Settings:
    """Get application settings singleton."""
    return Settings()


def get_yaml_config(config_path: Optional[str] = None) -> YAMLConfig:
    """Get YAML configuration."""
    return YAMLConfig(config_path)
