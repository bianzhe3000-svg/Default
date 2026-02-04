"""Logging configuration for the podcast automation system."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_loggers: dict[str, logging.Logger] = {}


def setup_logging(
    log_dir: str = "./logs",
    log_level: str = "INFO",
    log_file: str = "podcast-automation.log",
    max_size_mb: int = 50,
    backup_count: int = 5,
) -> None:
    """Set up application-wide logging configuration."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger("podcast_automation")
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Clear existing handlers
    root_logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler with rotation
    file_handler = RotatingFileHandler(
        log_path / log_file,
        maxBytes=max_size_mb * 1024 * 1024,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """Get a named logger for a module."""
    if name not in _loggers:
        _loggers[name] = logging.getLogger(f"podcast_automation.{name}")
    return _loggers[name]
