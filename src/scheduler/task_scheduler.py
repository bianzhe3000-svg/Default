"""Task scheduler for automated podcast processing."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import Settings, YAMLConfig
from src.database import get_session_factory
from src.models.podcast import Episode, Podcast, ProcessingStatus
from src.models.task import TaskLog, TaskStatus, TaskType
from src.modules.audio_transcriber import AudioTranscriber
from src.modules.content_analyzer import ContentAnalyzer
from src.modules.markdown_generator import MarkdownGenerator
from src.modules.rss_validator import RSSValidator
from src.utils.logger import get_logger

logger = get_logger("scheduler")


class PodcastScheduler:
    """Manages scheduled podcast processing tasks."""

    def __init__(self, settings: Settings, yaml_config: Optional[YAMLConfig] = None):
        self.settings = settings
        self.yaml_config = yaml_config
        self.scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)
        self._is_running = False

        # Initialize processing components
        self.rss_validator = RSSValidator(timeout=10.0)
        self.transcriber = AudioTranscriber(
            api_key=settings.openai_api_key,
            api_base=settings.openai_api_base,
            model=settings.whisper_model,
            temp_dir=settings.audio_temp_dir,
        )
        self.analyzer = ContentAnalyzer(
            api_key=settings.openai_api_key,
            api_base=settings.openai_api_base,
            model=settings.openai_model,
        )
        self.md_generator = MarkdownGenerator(
            summaries_dir=settings.summaries_dir,
        )

    def start(self):
        """Start the scheduler with configured cron jobs."""
        if self._is_running:
            logger.warning("Scheduler is already running")
            return

        # Add main crawl job
        trigger = CronTrigger(
            hour=self.settings.scheduler_cron_hour,
            minute=self.settings.scheduler_cron_minute,
            timezone=self.settings.scheduler_timezone,
        )
        self.scheduler.add_job(
            self._scheduled_crawl,
            trigger=trigger,
            id="daily_crawl",
            name="Daily Podcast Crawl",
            replace_existing=True,
            misfire_grace_time=3600,
        )

        self.scheduler.start()
        self._is_running = True
        logger.info(
            "Scheduler started. Daily crawl at %02d:%02d (%s)",
            self.settings.scheduler_cron_hour,
            self.settings.scheduler_cron_minute,
            self.settings.scheduler_timezone,
        )

    def stop(self):
        """Stop the scheduler."""
        if self._is_running:
            self.scheduler.shutdown(wait=False)
            self._is_running = False
            logger.info("Scheduler stopped")

    @property
    def is_running(self) -> bool:
        return self._is_running

    def get_jobs(self) -> list[dict]:
        """Get list of scheduled jobs."""
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "next_run": str(job.next_run_time) if job.next_run_time else None,
                    "trigger": str(job.trigger),
                }
            )
        return jobs

    async def _scheduled_crawl(self):
        """Execute the scheduled crawl and processing pipeline."""
        await self.run_crawl(trigger_source="scheduler")

    async def run_crawl(self, trigger_source: str = "manual") -> dict:
        """Run the full crawl and processing pipeline.

        Args:
            trigger_source: What triggered this crawl ('scheduler' or 'manual').

        Returns:
            Dictionary with processing results.
        """
        session_factory = get_session_factory()
        async with session_factory() as session:
            # Create task log
            task_log = TaskLog(
                task_type=TaskType.SCHEDULED_CRAWL
                if trigger_source == "scheduler"
                else TaskType.MANUAL_CRAWL,
                status=TaskStatus.RUNNING,
                trigger_source=trigger_source,
            )
            session.add(task_log)
            await session.commit()

            start_time = time.time()
            total_processed = 0
            total_succeeded = 0
            total_failed = 0
            errors = []

            try:
                # Get all enabled podcasts
                result = await session.execute(
                    select(Podcast).where(Podcast.enabled == True)  # noqa: E712
                )
                podcasts = list(result.scalars().all())

                if not podcasts:
                    logger.info("No enabled podcasts found")
                    task_log.status = TaskStatus.COMPLETED
                    task_log.details = json.dumps(
                        {"message": "No enabled podcasts"}, ensure_ascii=False
                    )
                    await session.commit()
                    return {"message": "No enabled podcasts", "processed": 0}

                logger.info("Processing %d podcasts", len(podcasts))

                # Process podcasts with concurrency limit
                semaphore = asyncio.Semaphore(self.settings.max_concurrent_feeds)
                tasks = [
                    self._process_podcast(session, podcast, semaphore)
                    for podcast in podcasts
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for r in results:
                    if isinstance(r, dict):
                        total_processed += r.get("processed", 0)
                        total_succeeded += r.get("succeeded", 0)
                        total_failed += r.get("failed", 0)
                        if r.get("errors"):
                            errors.extend(r["errors"])
                    elif isinstance(r, Exception):
                        total_failed += 1
                        errors.append(str(r))
                        logger.error("Podcast processing error: %s", r)

                # Update task log
                task_log.status = TaskStatus.COMPLETED
                task_log.episodes_processed = total_processed
                task_log.episodes_succeeded = total_succeeded
                task_log.episodes_failed = total_failed

            except Exception as exc:
                logger.error("Crawl pipeline error: %s", exc)
                task_log.status = TaskStatus.FAILED
                task_log.error_message = str(exc)
                errors.append(str(exc))

            finally:
                elapsed = time.time() - start_time
                task_log.finished_at = datetime.now(timezone.utc)
                task_log.duration_seconds = elapsed
                task_log.details = json.dumps(
                    {"errors": errors[:20]}, ensure_ascii=False
                )
                await session.commit()

            summary = {
                "task_id": task_log.id,
                "duration_seconds": elapsed,
                "podcasts_checked": len(podcasts),
                "episodes_processed": total_processed,
                "episodes_succeeded": total_succeeded,
                "episodes_failed": total_failed,
                "errors": errors[:10],
            }
            logger.info("Crawl completed: %s", json.dumps(summary, ensure_ascii=False))
            return summary

    async def _process_podcast(
        self,
        session: AsyncSession,
        podcast: Podcast,
        semaphore: asyncio.Semaphore,
    ) -> dict:
        """Process a single podcast: validate feed, find new episodes, process them."""
        async with semaphore:
            result = {"processed": 0, "succeeded": 0, "failed": 0, "errors": []}

            try:
                # Validate and parse RSS feed
                feed_result = await self.rss_validator.validate(podcast.rss_url)
                if not feed_result.is_valid:
                    result["errors"].append(
                        f"Invalid feed for {podcast.name}: {feed_result.errors}"
                    )
                    return result

                # Update podcast metadata
                podcast.last_checked_at = datetime.now(timezone.utc)
                if feed_result.description and not podcast.description:
                    podcast.description = feed_result.description
                if feed_result.image_url and not podcast.image_url:
                    podcast.image_url = feed_result.image_url

                # Get already processed episode GUIDs
                existing_result = await session.execute(
                    select(Episode.guid).where(Episode.podcast_id == podcast.id)
                )
                processed_guids = {row[0] for row in existing_result.all()}

                # Find new episodes
                new_episodes = self.rss_validator.get_new_episodes(
                    feed_result.episodes,
                    since_hours=self.settings.incremental_update_hours,
                    processed_guids=processed_guids,
                )

                logger.info(
                    "Podcast '%s': %d new episodes found",
                    podcast.name,
                    len(new_episodes),
                )

                # Process each new episode
                for ep_info in new_episodes:
                    result["processed"] += 1
                    try:
                        await self._process_episode(session, podcast, ep_info)
                        result["succeeded"] += 1
                    except Exception as exc:
                        result["failed"] += 1
                        result["errors"].append(
                            f"Episode '{ep_info.title}': {str(exc)}"
                        )
                        logger.error(
                            "Failed to process episode '%s': %s",
                            ep_info.title,
                            exc,
                        )

                await session.commit()

            except Exception as exc:
                result["errors"].append(f"Podcast '{podcast.name}': {str(exc)}")
                logger.error("Failed to process podcast '%s': %s", podcast.name, exc)

            return result

    async def _process_episode(
        self,
        session: AsyncSession,
        podcast: Podcast,
        ep_info,
    ):
        """Process a single episode: download, transcribe, analyze, generate markdown."""
        start_time = time.time()

        # Create episode record
        episode = Episode(
            podcast_id=podcast.id,
            guid=ep_info.guid,
            title=ep_info.title,
            description=ep_info.description,
            audio_url=ep_info.audio_url,
            audio_format=ep_info.audio_format,
            duration_seconds=ep_info.duration_seconds,
            file_size_bytes=ep_info.file_size_bytes,
            published_at=ep_info.published_at,
            processing_status=ProcessingStatus.DOWNLOADING,
        )
        session.add(episode)
        await session.flush()

        try:
            # Step 1: Download and transcribe
            episode.processing_status = ProcessingStatus.TRANSCRIBING
            await session.flush()

            transcription = await self.transcriber.transcribe_from_url(
                ep_info.audio_url
            )
            episode.transcript = transcription.text
            episode.transcription_time = transcription.duration_seconds

            # Step 2: Analyze content
            episode.processing_status = ProcessingStatus.ANALYZING
            await session.flush()

            analysis = await self.analyzer.analyze(
                transcript=transcription.text,
                episode_title=ep_info.title,
                podcast_name=podcast.name,
            )
            episode.summary = analysis.summary
            episode.key_points = analysis.key_points_json()
            episode.main_opinions = analysis.main_opinions_json()
            episode.knowledge_points = analysis.knowledge_points_json()
            episode.analysis_time = analysis.duration_seconds

            # Step 3: Generate Markdown
            episode.processing_status = ProcessingStatus.GENERATING
            await session.flush()

            content, filepath = self.md_generator.generate_and_save(
                podcast_name=podcast.name,
                episode_title=ep_info.title,
                analysis=analysis,
                published_at=ep_info.published_at,
                duration_seconds=ep_info.duration_seconds,
                audio_url=ep_info.audio_url,
            )
            episode.markdown_path = str(filepath)

            # Complete
            episode.processing_status = ProcessingStatus.COMPLETED
            episode.total_processing_time = time.time() - start_time

            logger.info(
                "Episode '%s' processed successfully in %.1fs",
                ep_info.title,
                episode.total_processing_time,
            )

        except Exception as exc:
            episode.processing_status = ProcessingStatus.FAILED
            episode.error_message = str(exc)
            episode.total_processing_time = time.time() - start_time
            logger.error("Episode processing failed: %s", exc)
            raise

    async def process_single_episode(
        self,
        podcast_id: int,
        episode_id: int,
    ) -> dict:
        """Re-process a single episode by ID."""
        session_factory = get_session_factory()
        async with session_factory() as session:
            podcast = await session.get(Podcast, podcast_id)
            episode = await session.get(Episode, episode_id)

            if not podcast or not episode:
                raise ValueError("Podcast or episode not found")

            from src.modules.rss_validator import EpisodeInfo

            ep_info = EpisodeInfo(
                guid=episode.guid,
                title=episode.title,
                description=episode.description or "",
                audio_url=episode.audio_url,
                audio_format=episode.audio_format or "",
                duration_seconds=episode.duration_seconds or 0,
                published_at=episode.published_at,
            )

            # Reset status
            episode.processing_status = ProcessingStatus.PENDING
            episode.error_message = None

            try:
                await self._process_episode(session, podcast, ep_info)
                await session.commit()
                return {"status": "completed", "episode_id": episode.id}
            except Exception as exc:
                await session.commit()
                return {
                    "status": "failed",
                    "episode_id": episode.id,
                    "error": str(exc),
                }
