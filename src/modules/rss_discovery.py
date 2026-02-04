"""RSS Feed discovery service using iTunes, Spotify and other podcast APIs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

import httpx
from fuzzywuzzy import fuzz

from src.utils.logger import get_logger
from src.utils.retry import async_retry

logger = get_logger("rss_discovery")

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"


@dataclass
class PodcastSearchResult:
    """Represents a podcast search result."""

    name: str
    rss_url: str
    author: str = ""
    description: str = ""
    image_url: str = ""
    genre: str = ""
    language: str = ""
    match_score: float = 0.0
    source: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "rss_url": self.rss_url,
            "author": self.author,
            "description": self.description,
            "image_url": self.image_url,
            "genre": self.genre,
            "language": self.language,
            "match_score": self.match_score,
            "source": self.source,
        }


class RSSDiscoveryService:
    """Discovers podcast RSS feeds through multiple platform APIs."""

    def __init__(
        self,
        itunes_enabled: bool = True,
        spotify_client_id: str = "",
        spotify_client_secret: str = "",
        timeout: float = 10.0,
    ):
        self.itunes_enabled = itunes_enabled
        self.spotify_client_id = spotify_client_id
        self.spotify_client_secret = spotify_client_secret
        self.timeout = timeout
        self._spotify_token: Optional[str] = None

    async def search(
        self, query: str, limit: int = 10
    ) -> list[PodcastSearchResult]:
        """Search for podcasts across all enabled platforms.

        Args:
            query: Podcast name or keyword to search for.
            limit: Maximum number of results to return.

        Returns:
            List of PodcastSearchResult sorted by match score.
        """
        results: list[PodcastSearchResult] = []
        tasks = []

        if self.itunes_enabled:
            tasks.append(self._search_itunes(query, limit))

        if self.spotify_client_id and self.spotify_client_secret:
            tasks.append(self._search_spotify(query, limit))

        if tasks:
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            for result in gathered:
                if isinstance(result, list):
                    results.extend(result)
                elif isinstance(result, Exception):
                    logger.error("Search task failed: %s", result)

        # Deduplicate by RSS URL
        seen_urls: set[str] = set()
        unique_results = []
        for r in results:
            if r.rss_url not in seen_urls:
                seen_urls.add(r.rss_url)
                # Compute fuzzy match score
                r.match_score = fuzz.token_sort_ratio(query, r.name) / 100.0
                unique_results.append(r)

        # Sort by match score descending
        unique_results.sort(key=lambda x: x.match_score, reverse=True)
        return unique_results[:limit]

    @async_retry(max_attempts=3, backoff_base=2, retryable_exceptions=(httpx.HTTPError,))
    async def _search_itunes(
        self, query: str, limit: int
    ) -> list[PodcastSearchResult]:
        """Search iTunes Podcast API."""
        results = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            params = {
                "term": query,
                "media": "podcast",
                "limit": limit,
                "country": "CN",
            }
            response = await client.get(ITUNES_SEARCH_URL, params=params)
            response.raise_for_status()
            data = response.json()

            for item in data.get("results", []):
                feed_url = item.get("feedUrl", "")
                if not feed_url:
                    continue
                results.append(
                    PodcastSearchResult(
                        name=item.get("collectionName", ""),
                        rss_url=feed_url,
                        author=item.get("artistName", ""),
                        description=item.get("description", "")
                        or item.get("collectionName", ""),
                        image_url=item.get("artworkUrl600", "")
                        or item.get("artworkUrl100", ""),
                        genre=item.get("primaryGenreName", ""),
                        language=item.get("country", ""),
                        source="itunes",
                    )
                )
        logger.info("iTunes search for '%s' returned %d results", query, len(results))
        return results

    @async_retry(max_attempts=3, backoff_base=2, retryable_exceptions=(httpx.HTTPError,))
    async def _search_spotify(
        self, query: str, limit: int
    ) -> list[PodcastSearchResult]:
        """Search Spotify Podcast API."""
        results = []
        token = await self._get_spotify_token()
        if not token:
            return results

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            headers = {"Authorization": f"Bearer {token}"}
            params = {
                "q": query,
                "type": "show",
                "market": "US",
                "limit": limit,
            }
            response = await client.get(
                "https://api.spotify.com/v1/search",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
            data = response.json()

            for item in data.get("shows", {}).get("items", []):
                # Spotify doesn't directly expose RSS URLs, but we record the show info
                # Users may need to find the RSS feed separately
                external_url = item.get("external_urls", {}).get("spotify", "")
                results.append(
                    PodcastSearchResult(
                        name=item.get("name", ""),
                        rss_url=external_url,  # Spotify URL as fallback
                        author=item.get("publisher", ""),
                        description=item.get("description", ""),
                        image_url=(item.get("images", [{}])[0].get("url", "")
                                   if item.get("images") else ""),
                        language=item.get("language", ""),
                        source="spotify",
                    )
                )
        logger.info("Spotify search for '%s' returned %d results", query, len(results))
        return results

    async def _get_spotify_token(self) -> Optional[str]:
        """Get Spotify API access token via client credentials flow."""
        if self._spotify_token:
            return self._spotify_token

        if not self.spotify_client_id or not self.spotify_client_secret:
            return None

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    "https://accounts.spotify.com/api/token",
                    data={"grant_type": "client_credentials"},
                    auth=(self.spotify_client_id, self.spotify_client_secret),
                )
                response.raise_for_status()
                self._spotify_token = response.json().get("access_token")
                return self._spotify_token
        except httpx.HTTPError as exc:
            logger.error("Failed to get Spotify token: %s", exc)
            return None

    async def lookup_by_itunes_id(self, itunes_id: int) -> Optional[PodcastSearchResult]:
        """Look up a specific podcast by iTunes ID."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                params = {"id": itunes_id, "entity": "podcast"}
                response = await client.get(ITUNES_LOOKUP_URL, params=params)
                response.raise_for_status()
                data = response.json()
                results = data.get("results", [])
                if results:
                    item = results[0]
                    feed_url = item.get("feedUrl", "")
                    if feed_url:
                        return PodcastSearchResult(
                            name=item.get("collectionName", ""),
                            rss_url=feed_url,
                            author=item.get("artistName", ""),
                            image_url=item.get("artworkUrl600", ""),
                            source="itunes",
                        )
        except httpx.HTTPError as exc:
            logger.error("iTunes lookup failed for ID %d: %s", itunes_id, exc)
        return None
