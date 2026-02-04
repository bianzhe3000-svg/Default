"""Tests for the task scheduler module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Settings
from src.scheduler.task_scheduler import PodcastScheduler


class TestPodcastScheduler:
    """Test task scheduler functionality."""

    def setup_method(self):
        self.settings = Settings(
            OPENAI_API_KEY="test-key",
            DATABASE_URL="sqlite+aiosqlite:///:memory:",
            SUMMARIES_DIR="./test_summaries",
            AUDIO_TEMP_DIR="./test_temp",
            SCHEDULER_CRON_HOUR=23,
            SCHEDULER_CRON_MINUTE=0,
            SCHEDULER_TIMEZONE="Asia/Shanghai",
            MAX_CONCURRENT_FEEDS=5,
            INCREMENTAL_UPDATE_HOURS=24,
        )

    def test_scheduler_init(self):
        """Test scheduler initialization."""
        scheduler = PodcastScheduler(settings=self.settings)
        assert scheduler.is_running is False
        assert scheduler.settings == self.settings

    @pytest.mark.asyncio
    async def test_scheduler_start_stop(self):
        """Test scheduler start and stop."""
        scheduler = PodcastScheduler(settings=self.settings)
        scheduler.start()
        assert scheduler.is_running is True

        jobs = scheduler.get_jobs()
        assert len(jobs) == 1
        assert jobs[0]["id"] == "daily_crawl"

        scheduler.stop()
        assert scheduler.is_running is False

    @pytest.mark.asyncio
    async def test_scheduler_double_start(self):
        """Test that starting scheduler twice doesn't raise errors."""
        scheduler = PodcastScheduler(settings=self.settings)
        scheduler.start()
        scheduler.start()  # Should log warning but not fail
        assert scheduler.is_running is True
        scheduler.stop()

    @pytest.mark.asyncio
    async def test_scheduler_get_jobs_format(self):
        """Test the format of scheduled jobs list."""
        scheduler = PodcastScheduler(settings=self.settings)
        scheduler.start()

        jobs = scheduler.get_jobs()
        assert len(jobs) >= 1
        job = jobs[0]
        assert "id" in job
        assert "name" in job
        assert "next_run" in job
        assert "trigger" in job

        scheduler.stop()

    def test_scheduler_components_initialized(self):
        """Test that all processing components are initialized."""
        scheduler = PodcastScheduler(settings=self.settings)
        assert scheduler.rss_validator is not None
        assert scheduler.transcriber is not None
        assert scheduler.analyzer is not None
        assert scheduler.md_generator is not None

    def test_scheduler_stop_when_not_running(self):
        """Test that stopping a non-running scheduler is safe."""
        scheduler = PodcastScheduler(settings=self.settings)
        scheduler.stop()  # Should not raise
        assert scheduler.is_running is False
