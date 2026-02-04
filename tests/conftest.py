"""Shared test fixtures for the podcast automation system."""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.database import Base
from src.models.podcast import Episode, Podcast, ProcessingStatus
from src.models.task import TaskLog, TaskStatus, TaskType


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_engine():
    """Create an in-memory SQLite database engine for testing."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    """Create a database session for testing."""
    session_factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def sample_podcast(db_session):
    """Create a sample podcast in the test database."""
    podcast = Podcast(
        name="测试播客",
        rss_url="https://example.com/test-feed.xml",
        description="一个测试用的播客",
        author="测试作者",
        language="zh-CN",
        enabled=True,
    )
    db_session.add(podcast)
    await db_session.flush()
    return podcast


@pytest_asyncio.fixture
async def sample_episode(db_session, sample_podcast):
    """Create a sample episode in the test database."""
    episode = Episode(
        podcast_id=sample_podcast.id,
        guid="test-episode-guid-001",
        title="测试剧集标题",
        description="这是一个测试剧集",
        audio_url="https://example.com/audio/test.mp3",
        audio_format="mp3",
        duration_seconds=3600,
        published_at=datetime.now(timezone.utc) - timedelta(hours=2),
        processing_status=ProcessingStatus.PENDING,
    )
    db_session.add(episode)
    await db_session.flush()
    return episode


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def sample_rss_xml():
    """Sample RSS XML content for testing."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>测试播客节目</title>
    <link>https://example.com</link>
    <description>这是一个测试播客的描述</description>
    <language>zh-cn</language>
    <itunes:author>测试作者</itunes:author>
    <itunes:image href="https://example.com/image.jpg"/>
    <item>
      <title>第一集：测试内容</title>
      <guid>episode-001</guid>
      <pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>
      <description>第一集的描述内容</description>
      <enclosure url="https://example.com/ep001.mp3"
                 length="50000000"
                 type="audio/mpeg"/>
      <itunes:duration>01:30:00</itunes:duration>
    </item>
    <item>
      <title>第二集：更多测试</title>
      <guid>episode-002</guid>
      <pubDate>Tue, 02 Jan 2024 00:00:00 +0000</pubDate>
      <description>第二集的描述内容</description>
      <enclosure url="https://example.com/ep002.mp3"
                 length="40000000"
                 type="audio/mpeg"/>
      <itunes:duration>45:00</itunes:duration>
    </item>
  </channel>
</rss>"""


@pytest.fixture
def sample_opml_content():
    """Sample OPML content for testing."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head>
    <title>My Podcasts</title>
  </head>
  <body>
    <outline text="技术" title="技术">
      <outline type="rss"
               text="测试播客1"
               title="测试播客1"
               xmlUrl="https://example.com/feed1.xml"
               htmlUrl="https://example.com/podcast1"/>
      <outline type="rss"
               text="测试播客2"
               title="测试播客2"
               xmlUrl="https://example.com/feed2.xml"/>
    </outline>
    <outline type="rss"
             text="测试播客3"
             title="测试播客3"
             xmlUrl="https://example.com/feed3.xml"/>
  </body>
</opml>"""


@pytest.fixture
def sample_transcript():
    """Sample Chinese podcast transcript for testing."""
    return """
大家好，欢迎收听本期播客节目。今天我们要讨论的话题是人工智能在教育领域的应用。

首先，让我们来了解一下什么是人工智能。人工智能，简称AI，是计算机科学的一个分支，
它致力于开发能够模拟人类智能行为的系统。在教育领域，AI技术正在带来革命性的变化。

第一个重要的应用是个性化学习。通过分析学生的学习数据，AI系统能够为每个学生
定制个性化的学习计划。例如，自适应学习平台可以根据学生的掌握程度，自动调整
教学内容的难度和进度。

第二个应用是智能辅导系统。这类系统可以24小时在线，随时为学生解答问题。
它们不仅能回答简单的知识性问题，还能进行深入的讨论和指导。

第三个值得关注的领域是教育评估。传统的考试方式存在很多局限性，而AI技术
可以提供更加全面、准确的评估方式。例如，自然语言处理技术可以自动批改
作文，并提供详细的反馈。

当然，AI在教育中的应用也面临着一些挑战。数据隐私是一个重要的问题，
我们需要确保学生的个人信息得到充分保护。此外，技术公平性也是需要
关注的问题，我们不能让AI技术加剧教育不平等。

总的来说，人工智能为教育带来了巨大的机遇，但我们也需要审慎地对待
其中的挑战。感谢大家的收听，我们下期再见！
"""


@pytest.fixture
def mock_openai_client():
    """Mock OpenAI client for testing."""
    client = AsyncMock()

    # Mock chat completion
    mock_message = MagicMock()
    mock_message.content = '["要点1：AI个性化学习", "要点2：智能辅导系统", "要点3：教育评估"]'
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    client.chat.completions.create = AsyncMock(return_value=mock_completion)

    # Mock transcription
    client.audio.transcriptions.create = AsyncMock(return_value="这是转录的文本内容")

    return client
