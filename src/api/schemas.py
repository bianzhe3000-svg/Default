"""Pydantic schemas for API request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl


# --- Podcast Schemas ---

class PodcastCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=500, description="播客名称")
    rss_url: str = Field(..., description="RSS Feed URL")
    language: str = Field(default="zh-CN", description="语言代码")
    enabled: bool = Field(default=True, description="是否启用")


class PodcastResponse(BaseModel):
    id: int
    name: str
    rss_url: str
    description: Optional[str] = None
    author: Optional[str] = None
    language: str
    image_url: Optional[str] = None
    enabled: bool
    last_checked_at: Optional[datetime] = None
    created_at: datetime
    episode_count: int = 0

    model_config = {"from_attributes": True}


class PodcastListResponse(BaseModel):
    total: int
    items: list[PodcastResponse]


# --- Episode Schemas ---

class EpisodeResponse(BaseModel):
    id: int
    podcast_id: int
    guid: str
    title: str
    description: Optional[str] = None
    audio_url: str
    audio_format: Optional[str] = None
    duration_seconds: Optional[int] = None
    published_at: Optional[datetime] = None
    processing_status: str
    summary: Optional[str] = None
    markdown_path: Optional[str] = None
    total_processing_time: Optional[float] = None
    error_message: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class EpisodeListResponse(BaseModel):
    total: int
    items: list[EpisodeResponse]


class EpisodeDetailResponse(EpisodeResponse):
    key_points: Optional[str] = None
    main_opinions: Optional[str] = None
    knowledge_points: Optional[str] = None
    transcript: Optional[str] = None
    transcription_time: Optional[float] = None
    analysis_time: Optional[float] = None


# --- Search Schemas ---

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="搜索关键词")
    limit: int = Field(default=10, ge=1, le=50, description="最大结果数")


class SearchResultItem(BaseModel):
    name: str
    rss_url: str
    author: str = ""
    description: str = ""
    image_url: str = ""
    match_score: float = 0.0
    source: str = ""


class SearchResponse(BaseModel):
    query: str
    total: int
    results: list[SearchResultItem]


# --- OPML Schemas ---

class OPMLImportResponse(BaseModel):
    total_found: int
    imported: int
    skipped: int
    errors: list[str] = []


# --- Task Schemas ---

class TaskLogResponse(BaseModel):
    id: int
    task_type: str
    status: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    episodes_processed: int
    episodes_succeeded: int
    episodes_failed: int
    retry_count: int
    error_message: Optional[str] = None
    trigger_source: Optional[str] = None

    model_config = {"from_attributes": True}


class TaskListResponse(BaseModel):
    total: int
    items: list[TaskLogResponse]


# --- Crawl Schemas ---

class CrawlResponse(BaseModel):
    task_id: int
    duration_seconds: float
    podcasts_checked: int
    episodes_processed: int
    episodes_succeeded: int
    episodes_failed: int
    errors: list[str] = []


# --- Document Schemas ---

class DocumentInfo(BaseModel):
    path: str
    relative_path: str
    filename: str
    size_bytes: int
    modified_at: str
    podcast_name: str


class DocumentListResponse(BaseModel):
    total: int
    items: list[DocumentInfo]


class DocumentContentResponse(BaseModel):
    path: str
    content: str
    html_preview: Optional[str] = None


# --- Scheduler Schemas ---

class SchedulerStatusResponse(BaseModel):
    is_running: bool
    jobs: list[dict]


# --- Memory Schemas ---

class MemoryStatsResponse(BaseModel):
    current_rss_mb: float = 0
    min_rss_mb: float = 0
    max_rss_mb: float = 0
    avg_rss_mb: float = 0
    samples: int = 0
    limit_mb: int = 0


# --- General Schemas ---

class HealthResponse(BaseModel):
    status: str
    version: str
    uptime_seconds: float


class ErrorResponse(BaseModel):
    detail: str
    error_code: Optional[str] = None
