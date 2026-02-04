"""FastAPI route definitions for the podcast automation system."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import (
    CrawlResponse,
    DocumentContentResponse,
    DocumentInfo,
    DocumentListResponse,
    EpisodeDetailResponse,
    EpisodeListResponse,
    EpisodeResponse,
    ErrorResponse,
    HealthResponse,
    MemoryStatsResponse,
    OPMLImportResponse,
    PodcastCreate,
    PodcastListResponse,
    PodcastResponse,
    SchedulerStatusResponse,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
    TaskListResponse,
    TaskLogResponse,
)
from src.database import get_db_session
from src.models.podcast import Episode, Podcast
from src.models.task import TaskLog
from src.utils.logger import get_logger

logger = get_logger("api")

router = APIRouter(prefix="/api", tags=["podcast-automation"])

# These will be set during app startup
_scheduler = None
_memory_monitor = None
_discovery_service = None
_opml_parser = None
_md_generator = None
_start_time = None


def set_dependencies(scheduler, memory_monitor, discovery_service, opml_parser, md_generator):
    """Inject dependencies from the main application."""
    global _scheduler, _memory_monitor, _discovery_service, _opml_parser, _md_generator, _start_time
    _scheduler = scheduler
    _memory_monitor = memory_monitor
    _discovery_service = discovery_service
    _opml_parser = opml_parser
    _md_generator = md_generator
    _start_time = datetime.now()


# --- Health Check ---

@router.get("/health", response_model=HealthResponse)
async def health_check():
    """System health check endpoint."""
    uptime = (datetime.now() - _start_time).total_seconds() if _start_time else 0
    return HealthResponse(
        status="healthy",
        version="1.0.0",
        uptime_seconds=uptime,
    )


# --- Podcast CRUD ---

@router.get("/podcasts", response_model=PodcastListResponse)
async def list_podcasts(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
):
    """List all registered podcasts."""
    total_result = await session.execute(select(func.count(Podcast.id)))
    total = total_result.scalar() or 0

    result = await session.execute(
        select(Podcast).offset(skip).limit(limit).order_by(Podcast.created_at.desc())
    )
    podcasts = result.scalars().all()

    items = []
    for p in podcasts:
        ep_count_result = await session.execute(
            select(func.count(Episode.id)).where(Episode.podcast_id == p.id)
        )
        ep_count = ep_count_result.scalar() or 0
        resp = PodcastResponse(
            id=p.id,
            name=p.name,
            rss_url=p.rss_url,
            description=p.description,
            author=p.author,
            language=p.language,
            image_url=p.image_url,
            enabled=p.enabled,
            last_checked_at=p.last_checked_at,
            created_at=p.created_at,
            episode_count=ep_count,
        )
        items.append(resp)

    return PodcastListResponse(total=total, items=items)


@router.post("/podcasts", response_model=PodcastResponse, status_code=201)
async def add_podcast(
    data: PodcastCreate,
    session: AsyncSession = Depends(get_db_session),
):
    """Add a new podcast by RSS feed URL."""
    # Check for duplicate
    existing = await session.execute(
        select(Podcast).where(Podcast.rss_url == data.rss_url)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Podcast with this RSS URL already exists")

    # Validate the RSS feed
    from src.modules.rss_validator import RSSValidator

    validator = RSSValidator()
    validation = await validator.validate(data.rss_url)

    podcast = Podcast(
        name=data.name or validation.title,
        rss_url=data.rss_url,
        description=validation.description if validation.is_valid else None,
        author=validation.author if validation.is_valid else None,
        language=data.language,
        image_url=validation.image_url if validation.is_valid else None,
        website_url=validation.website_url if validation.is_valid else None,
        enabled=data.enabled,
    )
    session.add(podcast)
    await session.flush()

    return PodcastResponse(
        id=podcast.id,
        name=podcast.name,
        rss_url=podcast.rss_url,
        description=podcast.description,
        author=podcast.author,
        language=podcast.language,
        image_url=podcast.image_url,
        enabled=podcast.enabled,
        last_checked_at=podcast.last_checked_at,
        created_at=podcast.created_at,
        episode_count=0,
    )


@router.delete("/podcasts/{podcast_id}", status_code=204)
async def delete_podcast(
    podcast_id: int,
    session: AsyncSession = Depends(get_db_session),
):
    """Remove a podcast and all its episodes."""
    podcast = await session.get(Podcast, podcast_id)
    if not podcast:
        raise HTTPException(status_code=404, detail="Podcast not found")
    await session.delete(podcast)


@router.get("/podcasts/{podcast_id}", response_model=PodcastResponse)
async def get_podcast(
    podcast_id: int,
    session: AsyncSession = Depends(get_db_session),
):
    """Get a specific podcast by ID."""
    podcast = await session.get(Podcast, podcast_id)
    if not podcast:
        raise HTTPException(status_code=404, detail="Podcast not found")

    ep_count_result = await session.execute(
        select(func.count(Episode.id)).where(Episode.podcast_id == podcast_id)
    )
    ep_count = ep_count_result.scalar() or 0

    return PodcastResponse(
        id=podcast.id,
        name=podcast.name,
        rss_url=podcast.rss_url,
        description=podcast.description,
        author=podcast.author,
        language=podcast.language,
        image_url=podcast.image_url,
        enabled=podcast.enabled,
        last_checked_at=podcast.last_checked_at,
        created_at=podcast.created_at,
        episode_count=ep_count,
    )


# --- Episodes ---

@router.get("/podcasts/{podcast_id}/episodes", response_model=EpisodeListResponse)
async def list_episodes(
    podcast_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    status: Optional[str] = Query(None, description="Filter by processing status"),
    session: AsyncSession = Depends(get_db_session),
):
    """List episodes for a podcast."""
    podcast = await session.get(Podcast, podcast_id)
    if not podcast:
        raise HTTPException(status_code=404, detail="Podcast not found")

    query = select(Episode).where(Episode.podcast_id == podcast_id)
    count_query = select(func.count(Episode.id)).where(
        Episode.podcast_id == podcast_id
    )

    if status:
        query = query.where(Episode.processing_status == status)
        count_query = count_query.where(Episode.processing_status == status)

    total_result = await session.execute(count_query)
    total = total_result.scalar() or 0

    result = await session.execute(
        query.offset(skip).limit(limit).order_by(Episode.published_at.desc())
    )
    episodes = result.scalars().all()

    items = [
        EpisodeResponse(
            id=ep.id,
            podcast_id=ep.podcast_id,
            guid=ep.guid,
            title=ep.title,
            description=ep.description,
            audio_url=ep.audio_url,
            audio_format=ep.audio_format,
            duration_seconds=ep.duration_seconds,
            published_at=ep.published_at,
            processing_status=ep.processing_status.value,
            summary=ep.summary,
            markdown_path=ep.markdown_path,
            total_processing_time=ep.total_processing_time,
            error_message=ep.error_message,
            created_at=ep.created_at,
        )
        for ep in episodes
    ]

    return EpisodeListResponse(total=total, items=items)


@router.get("/episodes/{episode_id}", response_model=EpisodeDetailResponse)
async def get_episode_detail(
    episode_id: int,
    session: AsyncSession = Depends(get_db_session),
):
    """Get detailed episode information including analysis results."""
    episode = await session.get(Episode, episode_id)
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found")

    return EpisodeDetailResponse(
        id=episode.id,
        podcast_id=episode.podcast_id,
        guid=episode.guid,
        title=episode.title,
        description=episode.description,
        audio_url=episode.audio_url,
        audio_format=episode.audio_format,
        duration_seconds=episode.duration_seconds,
        published_at=episode.published_at,
        processing_status=episode.processing_status.value,
        summary=episode.summary,
        key_points=episode.key_points,
        main_opinions=episode.main_opinions,
        knowledge_points=episode.knowledge_points,
        transcript=episode.transcript,
        markdown_path=episode.markdown_path,
        transcription_time=episode.transcription_time,
        analysis_time=episode.analysis_time,
        total_processing_time=episode.total_processing_time,
        error_message=episode.error_message,
        created_at=episode.created_at,
    )


@router.post("/episodes/{episode_id}/reprocess")
async def reprocess_episode(
    episode_id: int,
    session: AsyncSession = Depends(get_db_session),
):
    """Re-process a specific episode."""
    episode = await session.get(Episode, episode_id)
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found")

    if not _scheduler:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")

    result = await _scheduler.process_single_episode(
        podcast_id=episode.podcast_id,
        episode_id=episode_id,
    )
    return result


# --- Search ---

@router.post("/podcasts/search", response_model=SearchResponse)
async def search_podcasts(data: SearchRequest):
    """Search for podcasts on iTunes, Spotify, etc."""
    if not _discovery_service:
        raise HTTPException(status_code=503, detail="Discovery service not initialized")

    results = await _discovery_service.search(data.query, data.limit)
    items = [
        SearchResultItem(
            name=r.name,
            rss_url=r.rss_url,
            author=r.author,
            description=r.description,
            image_url=r.image_url,
            match_score=r.match_score,
            source=r.source,
        )
        for r in results
    ]
    return SearchResponse(query=data.query, total=len(items), results=items)


# --- OPML Import ---

@router.post("/podcasts/import-opml", response_model=OPMLImportResponse)
async def import_opml(
    file: UploadFile = File(..., description="OPML file to import"),
    session: AsyncSession = Depends(get_db_session),
):
    """Import podcasts from an OPML file."""
    if not _opml_parser:
        raise HTTPException(status_code=503, detail="OPML parser not initialized")

    content = await file.read()
    try:
        entries = _opml_parser.parse_string(content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid OPML file: {exc}")

    imported = 0
    skipped = 0
    errors = []

    for entry in entries:
        if not entry.rss_url:
            skipped += 1
            continue

        existing = await session.execute(
            select(Podcast).where(Podcast.rss_url == entry.rss_url)
        )
        if existing.scalar_one_or_none():
            skipped += 1
            continue

        try:
            podcast = Podcast(
                name=entry.name,
                rss_url=entry.rss_url,
                language="zh-CN",
                enabled=True,
            )
            session.add(podcast)
            imported += 1
        except Exception as exc:
            errors.append(f"{entry.name}: {str(exc)}")

    return OPMLImportResponse(
        total_found=len(entries),
        imported=imported,
        skipped=skipped,
        errors=errors,
    )


# --- Crawl ---

@router.post("/crawl", response_model=CrawlResponse)
async def trigger_crawl():
    """Manually trigger a crawl of all enabled podcasts."""
    if not _scheduler:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")

    result = await _scheduler.run_crawl(trigger_source="manual_api")
    return CrawlResponse(**result)


# --- Documents ---

@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(
    podcast_name: Optional[str] = Query(None, description="Filter by podcast name"),
):
    """List all generated Markdown documents."""
    if not _md_generator:
        raise HTTPException(status_code=503, detail="Markdown generator not initialized")

    docs = _md_generator.list_documents(podcast_name)
    items = [DocumentInfo(**d) for d in docs]
    return DocumentListResponse(total=len(items), items=items)


@router.get("/documents/view")
async def view_document(
    path: str = Query(..., description="Relative path to the document"),
    format: str = Query("markdown", description="Output format: markdown or html"),
):
    """View a generated Markdown document."""
    if not _md_generator:
        raise HTTPException(status_code=503, detail="Markdown generator not initialized")

    try:
        from pathlib import Path as PathLib

        full_path = PathLib(_md_generator.summaries_dir) / path
        content = _md_generator.read_document(str(full_path))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Document not found")

    html_preview = None
    if format == "html":
        html_preview = _md_generator.export_to_html(content)

    return DocumentContentResponse(
        path=path,
        content=content,
        html_preview=html_preview,
    )


@router.get("/documents/export")
async def export_document(
    path: str = Query(..., description="Relative path to the document"),
    format: str = Query("markdown", description="Export format: markdown or pdf"),
):
    """Export a document as Markdown or PDF."""
    if not _md_generator:
        raise HTTPException(status_code=503, detail="Markdown generator not initialized")

    from fastapi.responses import Response
    from pathlib import Path as PathLib

    try:
        full_path = PathLib(_md_generator.summaries_dir) / path
        content = _md_generator.read_document(str(full_path))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Document not found")

    if format == "pdf":
        try:
            html = _md_generator.export_to_html(content)
            # Wrap in basic HTML for PDF conversion
            full_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
       max-width: 800px; margin: 0 auto; padding: 20px; line-height: 1.6; }}
h1 {{ color: #333; }} h2 {{ color: #555; border-bottom: 1px solid #eee; }}
blockquote {{ border-left: 3px solid #ccc; padding-left: 10px; color: #666; }}
code {{ background: #f5f5f5; padding: 2px 5px; border-radius: 3px; }}
</style>
</head><body>{html}</body></html>"""

            from weasyprint import HTML

            pdf_bytes = HTML(string=full_html).write_pdf()
            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f'attachment; filename="{PathLib(path).stem}.pdf"'
                },
            )
        except ImportError:
            raise HTTPException(
                status_code=501,
                detail="PDF export requires weasyprint. Install it with: pip install weasyprint",
            )
    else:
        return Response(
            content=content.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{PathLib(path).name}"'
            },
        )


# --- Task Logs ---

@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    status: Optional[str] = Query(None, description="Filter by task status"),
    session: AsyncSession = Depends(get_db_session),
):
    """List task execution logs."""
    query = select(TaskLog)
    count_query = select(func.count(TaskLog.id))

    if status:
        query = query.where(TaskLog.status == status)
        count_query = count_query.where(TaskLog.status == status)

    total_result = await session.execute(count_query)
    total = total_result.scalar() or 0

    result = await session.execute(
        query.offset(skip).limit(limit).order_by(TaskLog.started_at.desc())
    )
    tasks = result.scalars().all()

    items = [
        TaskLogResponse(
            id=t.id,
            task_type=t.task_type.value,
            status=t.status.value,
            started_at=t.started_at,
            finished_at=t.finished_at,
            duration_seconds=t.duration_seconds,
            episodes_processed=t.episodes_processed,
            episodes_succeeded=t.episodes_succeeded,
            episodes_failed=t.episodes_failed,
            retry_count=t.retry_count,
            error_message=t.error_message,
            trigger_source=t.trigger_source,
        )
        for t in tasks
    ]

    return TaskListResponse(total=total, items=items)


# --- Scheduler ---

@router.get("/scheduler/status", response_model=SchedulerStatusResponse)
async def scheduler_status():
    """Get scheduler status and scheduled jobs."""
    if not _scheduler:
        return SchedulerStatusResponse(is_running=False, jobs=[])

    return SchedulerStatusResponse(
        is_running=_scheduler.is_running,
        jobs=_scheduler.get_jobs(),
    )


@router.post("/scheduler/start")
async def start_scheduler():
    """Start the task scheduler."""
    if not _scheduler:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")
    _scheduler.start()
    return {"status": "started"}


@router.post("/scheduler/stop")
async def stop_scheduler():
    """Stop the task scheduler."""
    if not _scheduler:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")
    _scheduler.stop()
    return {"status": "stopped"}


# --- Memory ---

@router.get("/memory", response_model=MemoryStatsResponse)
async def memory_stats():
    """Get memory usage statistics."""
    if not _memory_monitor:
        raise HTTPException(status_code=503, detail="Memory monitor not initialized")

    stats = _memory_monitor.get_stats()
    if not stats:
        return MemoryStatsResponse()
    return MemoryStatsResponse(**stats)
