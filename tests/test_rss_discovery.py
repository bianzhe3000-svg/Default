"""Tests for the RSS feed discovery service."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.modules.rss_discovery import PodcastSearchResult, RSSDiscoveryService


def _make_mock_client(mock_response):
    """Create a properly mocked httpx.AsyncClient for context manager use."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.post = AsyncMock(return_value=mock_response)
    # Support `async with httpx.AsyncClient() as client:`
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


class TestRSSDiscoveryService:
    """Test podcast search across platforms."""

    def setup_method(self):
        self.service = RSSDiscoveryService(itunes_enabled=True)

    @pytest.mark.asyncio
    async def test_search_itunes_success(self):
        """Test successful iTunes search."""
        mock_data = {
            "resultCount": 2,
            "results": [
                {
                    "collectionName": "测试播客",
                    "feedUrl": "https://example.com/feed.xml",
                    "artistName": "测试作者",
                    "artworkUrl600": "https://example.com/img.jpg",
                    "primaryGenreName": "Technology",
                    "country": "CN",
                },
                {
                    "collectionName": "Another Podcast",
                    "feedUrl": "https://example.com/feed2.xml",
                    "artistName": "Author",
                    "artworkUrl600": "",
                    "primaryGenreName": "News",
                },
            ],
        }

        mock_response = MagicMock()
        mock_response.json.return_value = mock_data
        mock_response.raise_for_status = MagicMock()

        mock_client = _make_mock_client(mock_response)

        with patch("src.modules.rss_discovery.httpx.AsyncClient", return_value=mock_client):
            results = await self.service.search("测试播客", limit=10)

        assert len(results) == 2
        assert results[0].source == "itunes"

    @pytest.mark.asyncio
    async def test_search_empty_results(self):
        """Test search with no results."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"resultCount": 0, "results": []}
        mock_response.raise_for_status = MagicMock()

        mock_client = _make_mock_client(mock_response)

        with patch("src.modules.rss_discovery.httpx.AsyncClient", return_value=mock_client):
            results = await self.service.search("nonexistent_podcast")

        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_search_deduplicates_results(self):
        """Test that duplicate RSS URLs are removed."""
        mock_data = {
            "resultCount": 2,
            "results": [
                {
                    "collectionName": "Same Podcast",
                    "feedUrl": "https://example.com/same-feed.xml",
                    "artistName": "Author",
                },
                {
                    "collectionName": "Same Podcast Copy",
                    "feedUrl": "https://example.com/same-feed.xml",
                    "artistName": "Author",
                },
            ],
        }

        mock_response = MagicMock()
        mock_response.json.return_value = mock_data
        mock_response.raise_for_status = MagicMock()

        mock_client = _make_mock_client(mock_response)

        with patch("src.modules.rss_discovery.httpx.AsyncClient", return_value=mock_client):
            results = await self.service.search("Same Podcast")

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_search_fuzzy_matching(self):
        """Test that results are sorted by fuzzy match score."""
        mock_data = {
            "resultCount": 2,
            "results": [
                {
                    "collectionName": "Something Else",
                    "feedUrl": "https://example.com/other.xml",
                    "artistName": "Author",
                },
                {
                    "collectionName": "AI教育播客",
                    "feedUrl": "https://example.com/ai-edu.xml",
                    "artistName": "Author",
                },
            ],
        }

        mock_response = MagicMock()
        mock_response.json.return_value = mock_data
        mock_response.raise_for_status = MagicMock()

        mock_client = _make_mock_client(mock_response)

        with patch("src.modules.rss_discovery.httpx.AsyncClient", return_value=mock_client):
            results = await self.service.search("AI教育播客")

        assert len(results) == 2
        assert results[0].match_score >= results[1].match_score

    def test_podcast_search_result_to_dict(self):
        """Test PodcastSearchResult serialization."""
        result = PodcastSearchResult(
            name="Test",
            rss_url="https://example.com/feed.xml",
            author="Author",
            source="itunes",
            match_score=0.95,
        )
        d = result.to_dict()
        assert d["name"] == "Test"
        assert d["rss_url"] == "https://example.com/feed.xml"
        assert d["match_score"] == 0.95
