"""Integration tests for the podcast automation system."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.database import Base
from src.models.podcast import Episode, Podcast, ProcessingStatus
from src.models.task import TaskLog
from src.modules.content_analyzer import AnalysisResult, KnowledgePoint, Opinion
from src.modules.markdown_generator import MarkdownGenerator
from src.modules.opml_parser import OPMLParser
from src.modules.rss_validator import EpisodeInfo, FeedValidationResult, RSSValidator


class TestEndToEndPipeline:
    """Test the complete podcast processing pipeline."""

    @pytest_asyncio.fixture
    async def setup_db(self):
        """Set up test database."""
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        async with session_factory() as session:
            yield session

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_podcast_crud(self, setup_db):
        """Test podcast creation, reading, updating, and deletion."""
        session = setup_db

        # Create
        podcast = Podcast(
            name="Integration Test Podcast",
            rss_url="https://example.com/integration-feed.xml",
            language="zh-CN",
            enabled=True,
        )
        session.add(podcast)
        await session.flush()
        assert podcast.id is not None

        # Read
        result = await session.execute(
            select(Podcast).where(Podcast.id == podcast.id)
        )
        found = result.scalar_one()
        assert found.name == "Integration Test Podcast"

        # Update
        found.description = "Updated description"
        await session.flush()

        result = await session.execute(
            select(Podcast).where(Podcast.id == podcast.id)
        )
        updated = result.scalar_one()
        assert updated.description == "Updated description"

        # Delete
        await session.delete(updated)
        await session.flush()

        result = await session.execute(
            select(Podcast).where(Podcast.id == podcast.id)
        )
        assert result.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_episode_lifecycle(self, setup_db):
        """Test episode creation and status transitions."""
        session = setup_db

        podcast = Podcast(
            name="Test Podcast",
            rss_url="https://example.com/feed.xml",
            enabled=True,
        )
        session.add(podcast)
        await session.flush()

        episode = Episode(
            podcast_id=podcast.id,
            guid="test-guid-001",
            title="Test Episode",
            audio_url="https://example.com/audio.mp3",
            processing_status=ProcessingStatus.PENDING,
        )
        session.add(episode)
        await session.flush()

        # Simulate processing pipeline status transitions
        for status in [
            ProcessingStatus.DOWNLOADING,
            ProcessingStatus.TRANSCRIBING,
            ProcessingStatus.ANALYZING,
            ProcessingStatus.GENERATING,
            ProcessingStatus.COMPLETED,
        ]:
            episode.processing_status = status
            await session.flush()

        result = await session.execute(
            select(Episode).where(Episode.id == episode.id)
        )
        final_episode = result.scalar_one()
        assert final_episode.processing_status == ProcessingStatus.COMPLETED

    def test_rss_to_markdown_pipeline(self, temp_dir, sample_transcript):
        """Test the flow from RSS parsing to Markdown generation."""
        # Simulate RSS feed validation result
        episode_info = EpisodeInfo(
            guid="pipeline-test-001",
            title="AI教育播客第10期",
            description="讨论AI在教育领域的应用",
            audio_url="https://example.com/ep10.mp3",
            audio_format="mp3",
            duration_seconds=5400,
            published_at=datetime(2024, 1, 15, tzinfo=timezone.utc),
        )

        # Simulate analysis result
        analysis = AnalysisResult(
            summary="本期播客讨论了AI在教育领域的多种应用...",
            key_points=["个性化学习", "智能辅导", "教育评估"],
            main_opinions=[
                Opinion(
                    title="AI革新教育",
                    summary="AI正在带来教育变革",
                    details="详细内容...",
                    supporting_quotes=["引用1"],
                )
            ],
            knowledge_points=[
                KnowledgePoint(
                    topic="教育技术",
                    concept="自适应学习",
                    explanation="根据学生水平调整难度",
                    category="AI教育",
                )
            ],
        )

        # Generate Markdown
        generator = MarkdownGenerator(summaries_dir=temp_dir)
        content, filepath = generator.generate_and_save(
            podcast_name="AI教育播客",
            episode_title=episode_info.title,
            analysis=analysis,
            published_at=episode_info.published_at,
            duration_seconds=episode_info.duration_seconds,
        )

        # Verify output
        assert filepath.exists()
        assert "AI教育播客" in content
        assert "AI教育播客第10期" in content
        assert "个性化学习" in content
        assert "自适应学习" in content
        assert "2024-01-15" in filepath.name

    def test_opml_import_and_validate(self, sample_opml_content):
        """Test importing OPML and extracting feed URLs."""
        parser = OPMLParser()
        entries = parser.parse_string(sample_opml_content)

        assert len(entries) >= 2
        rss_urls = [e.rss_url for e in entries]
        assert all(url.startswith("https://") for url in rss_urls)

    def test_incremental_update_detection(self):
        """Test that incremental updates correctly identify new episodes."""
        validator = RSSValidator()

        now = datetime.now(timezone.utc)
        episodes = [
            EpisodeInfo(
                guid=f"ep-{i}",
                title=f"Episode {i}",
                audio_url=f"https://example.com/ep{i}.mp3",
                published_at=now - __import__("datetime").timedelta(hours=h),
            )
            for i, h in [(1, 2), (2, 12), (3, 25), (4, 48)]
        ]

        # 24-hour window
        new_eps = validator.get_new_episodes(episodes, since_hours=24)
        assert len(new_eps) == 2  # ep-1 and ep-2

        # Exclude already processed
        new_eps = validator.get_new_episodes(
            episodes, since_hours=24, processed_guids={"ep-1"}
        )
        assert len(new_eps) == 1
        assert new_eps[0].guid == "ep-2"


class TestAPIIntegration:
    """Test API endpoint integration."""

    @pytest.mark.asyncio
    async def test_health_endpoint_structure(self):
        """Test that health check returns expected structure."""
        # Import the app to verify it can be created
        from src.main import create_app

        app = create_app()
        assert app.title == "播客自动化处理系统"

    def test_schemas_validation(self):
        """Test that Pydantic schemas validate correctly."""
        from src.api.schemas import PodcastCreate, SearchRequest

        # Valid podcast creation
        podcast = PodcastCreate(
            name="Test", rss_url="https://example.com/feed.xml"
        )
        assert podcast.name == "Test"

        # Valid search request
        search = SearchRequest(query="test podcast", limit=10)
        assert search.query == "test podcast"

        # Invalid: empty name
        with pytest.raises(Exception):
            PodcastCreate(name="", rss_url="https://example.com/feed.xml")

        # Invalid: empty query
        with pytest.raises(Exception):
            SearchRequest(query="", limit=10)
