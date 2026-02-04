"""Main application entry point for the podcast automation system."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.api.routes import router, set_dependencies
from src.config import get_settings, get_yaml_config
from src.database import close_db, init_db
from src.modules.markdown_generator import MarkdownGenerator
from src.modules.opml_parser import OPMLParser
from src.modules.rss_discovery import RSSDiscoveryService
from src.scheduler.task_scheduler import PodcastScheduler
from src.utils.logger import get_logger, setup_logging
from src.utils.memory_monitor import MemoryMonitor


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle manager."""
    settings = get_settings()
    yaml_config = get_yaml_config()

    # Setup logging
    setup_logging(
        log_dir=settings.log_dir,
        log_level=settings.log_level,
    )
    logger = get_logger("main")
    logger.info("Starting Podcast Automation System v1.0.0")

    # Ensure directories exist
    Path(settings.summaries_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.audio_temp_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.log_dir).mkdir(parents=True, exist_ok=True)

    # Initialize database
    await init_db()
    logger.info("Database initialized")

    # Initialize components
    memory_monitor = MemoryMonitor(
        limit_mb=settings.memory_limit_mb,
        check_interval=settings.memory_check_interval,
    )

    scheduler = PodcastScheduler(settings=settings, yaml_config=yaml_config)

    discovery_service = RSSDiscoveryService(
        itunes_enabled=settings.itunes_api_enabled,
        spotify_client_id=settings.spotify_client_id,
        spotify_client_secret=settings.spotify_client_secret,
    )

    opml_parser = OPMLParser()

    md_generator = MarkdownGenerator(
        summaries_dir=settings.summaries_dir,
    )

    # Inject dependencies into routes
    set_dependencies(
        scheduler=scheduler,
        memory_monitor=memory_monitor,
        discovery_service=discovery_service,
        opml_parser=opml_parser,
        md_generator=md_generator,
    )

    # Start background services
    await memory_monitor.start_monitoring()
    scheduler.start()

    # Import podcasts from YAML config if any
    if yaml_config.podcasts:
        from sqlalchemy import select
        from src.database import get_session_factory
        from src.models.podcast import Podcast

        session_factory = get_session_factory()
        async with session_factory() as session:
            for pc in yaml_config.podcasts:
                if not pc.get("enabled", True):
                    continue
                rss_url = pc.get("rss_url", "")
                if not rss_url:
                    continue
                existing = await session.execute(
                    select(Podcast).where(Podcast.rss_url == rss_url)
                )
                if existing.scalar_one_or_none():
                    continue
                podcast = Podcast(
                    name=pc.get("name", ""),
                    rss_url=rss_url,
                    language=pc.get("language", "zh-CN"),
                    enabled=True,
                )
                session.add(podcast)
            await session.commit()
        logger.info("Synced podcasts from YAML config")

    logger.info("All systems initialized. Server ready.")

    yield

    # Shutdown
    logger.info("Shutting down...")
    scheduler.stop()
    await memory_monitor.stop_monitoring()
    await close_db()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="播客自动化处理系统",
        description="Automated Podcast Content Processing System - "
        "RSS抓取、音频转录、内容分析、Markdown生成",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Include API router
    app.include_router(router)

    return app


app = create_app()


def main():
    """Run the application server."""
    settings = get_settings()
    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
