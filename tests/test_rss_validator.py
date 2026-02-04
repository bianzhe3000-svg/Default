"""Tests for the RSS feed validator module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.modules.rss_validator import EpisodeInfo, FeedValidationResult, RSSValidator


class TestRSSValidator:
    """Test RSS feed validation and parsing."""

    def setup_method(self):
        self.validator = RSSValidator(timeout=5.0)

    @pytest.mark.asyncio
    async def test_validate_valid_feed(self, sample_rss_xml):
        """Test validation of a valid RSS feed."""
        mock_response = MagicMock()
        mock_response.text = sample_rss_xml
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("src.modules.rss_validator.httpx.AsyncClient", return_value=mock_client):
            result = await self.validator.validate("https://example.com/feed.xml")

        assert result.is_valid is True
        assert result.title == "测试播客节目"
        assert result.episode_count == 2
        assert len(result.episodes) == 2
        assert result.episodes[0].title == "第一集：测试内容"
        assert result.episodes[0].audio_url == "https://example.com/ep001.mp3"
        assert result.episodes[0].audio_format == "mp3"

    @pytest.mark.asyncio
    async def test_validate_invalid_feed(self):
        """Test validation of an invalid RSS feed."""
        mock_response = MagicMock()
        mock_response.text = "<html><body>Not a feed</body></html>"
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("src.modules.rss_validator.httpx.AsyncClient", return_value=mock_client):
            result = await self.validator.validate("https://example.com/bad.xml")

        assert result.is_valid is False

    @pytest.mark.asyncio
    async def test_validate_network_error(self):
        """Test handling of network errors during validation."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection failed"))

        with patch("src.modules.rss_validator.httpx.AsyncClient", return_value=mock_client):
            # The validate method catches HTTP errors internally and returns invalid result
            result = await self.validator.validate("https://example.com/feed.xml")

        assert result.is_valid is False
        assert len(result.errors) > 0
        assert "Connection failed" in result.errors[0]

    def test_parse_duration_hhmmss(self):
        """Test parsing HH:MM:SS duration format."""
        assert self.validator._parse_duration("01:30:00") == 5400
        assert self.validator._parse_duration("00:45:30") == 2730

    def test_parse_duration_mmss(self):
        """Test parsing MM:SS duration format."""
        assert self.validator._parse_duration("45:00") == 2700
        assert self.validator._parse_duration("10:30") == 630

    def test_parse_duration_seconds(self):
        """Test parsing raw seconds duration."""
        assert self.validator._parse_duration("3600") == 3600

    def test_parse_duration_empty(self):
        """Test parsing empty duration."""
        assert self.validator._parse_duration("") == 0
        assert self.validator._parse_duration("invalid") == 0

    def test_is_audio_url(self):
        """Test audio URL detection."""
        assert self.validator._is_audio_url("https://example.com/audio.mp3") is True
        assert self.validator._is_audio_url("https://example.com/audio.m4a") is True
        assert self.validator._is_audio_url("https://example.com/audio.wav") is True
        assert self.validator._is_audio_url("https://example.com/page.html") is False
        assert self.validator._is_audio_url(
            "https://example.com/audio.mp3?token=abc"
        ) is True

    def test_detect_audio_format(self):
        """Test audio format detection."""
        assert (
            self.validator._detect_audio_format(
                "https://example.com/a.mp3", "audio/mpeg"
            )
            == "mp3"
        )
        assert (
            self.validator._detect_audio_format(
                "https://example.com/a.m4a", "audio/mp4"
            )
            == "m4a"
        )
        assert (
            self.validator._detect_audio_format(
                "https://example.com/audio", "audio/mpeg"
            )
            == "mp3"
        )

    def test_get_new_episodes_within_window(self):
        """Test incremental update detection within time window."""
        now = datetime.now(timezone.utc)
        episodes = [
            EpisodeInfo(
                guid="ep1",
                title="New Episode",
                published_at=now - timedelta(hours=2),
                audio_url="https://example.com/new.mp3",
            ),
            EpisodeInfo(
                guid="ep2",
                title="Old Episode",
                published_at=now - timedelta(hours=48),
                audio_url="https://example.com/old.mp3",
            ),
        ]

        new_episodes = self.validator.get_new_episodes(episodes, since_hours=24)
        assert len(new_episodes) == 1
        assert new_episodes[0].guid == "ep1"

    def test_get_new_episodes_excludes_processed(self):
        """Test that already-processed episodes are excluded."""
        now = datetime.now(timezone.utc)
        episodes = [
            EpisodeInfo(
                guid="ep1",
                title="Episode 1",
                published_at=now - timedelta(hours=2),
                audio_url="https://example.com/ep1.mp3",
            ),
            EpisodeInfo(
                guid="ep2",
                title="Episode 2",
                published_at=now - timedelta(hours=3),
                audio_url="https://example.com/ep2.mp3",
            ),
        ]

        new_episodes = self.validator.get_new_episodes(
            episodes, since_hours=24, processed_guids={"ep1"}
        )
        assert len(new_episodes) == 1
        assert new_episodes[0].guid == "ep2"

    def test_get_new_episodes_custom_window(self):
        """Test incremental update with custom time window."""
        now = datetime.now(timezone.utc)
        episodes = [
            EpisodeInfo(
                guid="ep1",
                title="Recent",
                published_at=now - timedelta(hours=6),
                audio_url="https://example.com/ep1.mp3",
            ),
            EpisodeInfo(
                guid="ep2",
                title="Semi-old",
                published_at=now - timedelta(hours=30),
                audio_url="https://example.com/ep2.mp3",
            ),
        ]

        # With 12-hour window, only ep1 is new
        new_12h = self.validator.get_new_episodes(episodes, since_hours=12)
        assert len(new_12h) == 1

        # With 48-hour window, both are new
        new_48h = self.validator.get_new_episodes(episodes, since_hours=48)
        assert len(new_48h) == 2
