"""Podcast and Episode database models."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class ProcessingStatus(str, enum.Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    TRANSCRIBING = "transcribing"
    ANALYZING = "analyzing"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"


class Podcast(Base):
    __tablename__ = "podcasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    rss_url: Mapped[str] = mapped_column(String(2000), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(String(500), nullable=True)
    language: Mapped[str] = mapped_column(String(10), default="zh-CN")
    image_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    website_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    episodes: Mapped[list[Episode]] = relationship(
        "Episode", back_populates="podcast", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Podcast(id={self.id}, name='{self.name}')>"


class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (
        Index("idx_episode_guid", "guid"),
        Index("idx_episode_published", "published_at"),
        Index("idx_episode_status", "processing_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    podcast_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("podcasts.id", ondelete="CASCADE"), nullable=False
    )
    guid: Mapped[str] = mapped_column(String(2000), nullable=False)
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    audio_format: Mapped[str | None] = mapped_column(String(10), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Processing fields
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        Enum(ProcessingStatus), default=ProcessingStatus.PENDING
    )
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_points: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON string
    main_opinions: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON string
    knowledge_points: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON string
    markdown_path: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    # Metrics
    transcription_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    analysis_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_processing_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    podcast: Mapped[Podcast] = relationship("Podcast", back_populates="episodes")

    def __repr__(self):
        return f"<Episode(id={self.id}, title='{self.title}')>"
