"""RSS Feed validation and parsing module."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import feedparser
import httpx

from src.utils.logger import get_logger
from src.utils.retry import async_retry

logger = get_logger("rss_validator")

SUPPORTED_AUDIO_FORMATS = {"mp3", "m4a", "wav", "ogg", "aac", "opus", "flac"}


@dataclass
class EpisodeInfo:
    """Parsed episode information from an RSS feed."""

    guid: str
    title: str
    description: str = ""
    audio_url: str = ""
    audio_format: str = ""
    duration_seconds: int = 0
    file_size_bytes: int = 0
    published_at: Optional[datetime] = None
    link: str = ""


@dataclass
class FeedValidationResult:
    """Result of RSS feed validation."""

    is_valid: bool
    feed_url: str
    title: str = ""
    description: str = ""
    author: str = ""
    language: str = ""
    image_url: str = ""
    website_url: str = ""
    episode_count: int = 0
    episodes: list[EpisodeInfo] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class RSSValidator:
    """Validates and parses RSS feeds for podcast content."""

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    @async_retry(max_attempts=3, backoff_base=2, retryable_exceptions=(httpx.HTTPError,))
    async def validate(self, feed_url: str) -> FeedValidationResult:
        """Validate an RSS feed URL and parse its contents.

        Args:
            feed_url: URL of the RSS feed to validate.

        Returns:
            FeedValidationResult with validation status and parsed data.
        """
        result = FeedValidationResult(is_valid=False, feed_url=feed_url)

        try:
            # Fetch the feed content
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True
            ) as client:
                response = await client.get(
                    feed_url,
                    headers={"User-Agent": "PodcastAutomation/1.0"},
                )
                response.raise_for_status()
                content = response.text
        except httpx.HTTPError as exc:
            result.errors.append(f"HTTP error fetching feed: {exc}")
            logger.error("Failed to fetch RSS feed %s: %s", feed_url, exc)
            return result

        # Parse the feed
        feed = feedparser.parse(content)

        if feed.bozo and not feed.entries:
            result.errors.append(
                f"Feed parsing error: {feed.bozo_exception}"
            )
            logger.error("Feed parsing error for %s: %s", feed_url, feed.bozo_exception)
            return result

        # Extract feed metadata
        feed_info = feed.feed
        result.title = feed_info.get("title", "")
        result.description = feed_info.get("subtitle", "") or feed_info.get(
            "summary", ""
        )
        result.author = feed_info.get("author", "") or feed_info.get(
            "itunes_author", ""
        )
        result.language = feed_info.get("language", "")
        result.website_url = feed_info.get("link", "")

        # Extract image
        if hasattr(feed_info, "image") and feed_info.image:
            result.image_url = feed_info.image.get("href", "")
        elif hasattr(feed_info, "itunes_image"):
            result.image_url = getattr(feed_info, "itunes_image", {}).get(
                "href", ""
            )

        if not result.title:
            result.errors.append("Feed has no title")
            return result

        # Parse episodes
        episodes = []
        for entry in feed.entries:
            episode = self._parse_episode(entry)
            if episode:
                episodes.append(episode)

        if not episodes:
            result.errors.append("Feed contains no episodes with audio content")
            return result

        result.episodes = episodes
        result.episode_count = len(episodes)
        result.is_valid = True

        logger.info(
            "Validated feed '%s' with %d episodes", result.title, result.episode_count
        )
        return result

    def _parse_episode(self, entry: dict) -> Optional[EpisodeInfo]:
        """Parse a single RSS feed entry into an EpisodeInfo."""
        # Find audio enclosure
        audio_url = ""
        audio_format = ""
        file_size = 0

        for enclosure in entry.get("enclosures", []):
            enc_type = enclosure.get("type", "")
            enc_url = enclosure.get("href", "") or enclosure.get("url", "")
            if "audio" in enc_type or self._is_audio_url(enc_url):
                audio_url = enc_url
                audio_format = self._detect_audio_format(enc_url, enc_type)
                try:
                    file_size = int(enclosure.get("length", 0))
                except (ValueError, TypeError):
                    file_size = 0
                break

        # Also check for media content
        if not audio_url:
            for media in entry.get("media_content", []):
                media_type = media.get("type", "")
                media_url = media.get("url", "")
                if "audio" in media_type or self._is_audio_url(media_url):
                    audio_url = media_url
                    audio_format = self._detect_audio_format(media_url, media_type)
                    break

        if not audio_url:
            return None

        # Parse publication date
        published_at = None
        published_str = entry.get("published", "") or entry.get("updated", "")
        if published_str:
            try:
                published_at = parsedate_to_datetime(published_str)
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        # Parse duration
        duration = 0
        duration_str = entry.get("itunes_duration", "")
        if duration_str:
            duration = self._parse_duration(str(duration_str))

        # Get GUID
        guid = entry.get("id", "") or entry.get("guid", "") or audio_url

        return EpisodeInfo(
            guid=guid,
            title=entry.get("title", "Untitled"),
            description=entry.get("summary", "") or entry.get("subtitle", ""),
            audio_url=audio_url,
            audio_format=audio_format,
            duration_seconds=duration,
            file_size_bytes=file_size,
            published_at=published_at,
            link=entry.get("link", ""),
        )

    @staticmethod
    def _is_audio_url(url: str) -> bool:
        """Check if a URL points to an audio file."""
        url_lower = url.lower().split("?")[0]
        return any(url_lower.endswith(f".{fmt}") for fmt in SUPPORTED_AUDIO_FORMATS)

    @staticmethod
    def _detect_audio_format(url: str, content_type: str) -> str:
        """Detect audio format from URL or content type."""
        url_lower = url.lower().split("?")[0]
        for fmt in SUPPORTED_AUDIO_FORMATS:
            if url_lower.endswith(f".{fmt}"):
                return fmt

        type_map = {
            "audio/mpeg": "mp3",
            "audio/mp3": "mp3",
            "audio/mp4": "m4a",
            "audio/x-m4a": "m4a",
            "audio/wav": "wav",
            "audio/ogg": "ogg",
            "audio/aac": "aac",
            "audio/opus": "opus",
            "audio/flac": "flac",
        }
        return type_map.get(content_type.lower(), "mp3")

    @staticmethod
    def _parse_duration(duration_str: str) -> int:
        """Parse iTunes duration string to seconds."""
        if not duration_str:
            return 0

        # Try parsing as integer (seconds)
        try:
            return int(duration_str)
        except ValueError:
            pass

        # Try parsing as HH:MM:SS or MM:SS
        parts = duration_str.split(":")
        try:
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            elif len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
        except (ValueError, IndexError):
            pass

        return 0

    def get_new_episodes(
        self,
        episodes: list[EpisodeInfo],
        since_hours: int = 24,
        processed_guids: set[str] | None = None,
    ) -> list[EpisodeInfo]:
        """Filter episodes to only include new ones since the given time window.

        Args:
            episodes: List of parsed episodes.
            since_hours: Number of hours to look back for new episodes.
            processed_guids: Set of already-processed episode GUIDs.

        Returns:
            List of new episodes not yet processed.
        """
        if processed_guids is None:
            processed_guids = set()

        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        new_episodes = []

        for ep in episodes:
            if ep.guid in processed_guids:
                continue
            if ep.published_at and ep.published_at >= cutoff:
                new_episodes.append(ep)
            elif not ep.published_at and ep.guid not in processed_guids:
                # Include episodes without a publish date if not already processed
                new_episodes.append(ep)

        logger.info(
            "Found %d new episodes out of %d total (since %d hours ago)",
            len(new_episodes),
            len(episodes),
            since_hours,
        )
        return new_episodes
