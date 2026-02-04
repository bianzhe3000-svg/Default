"""Audio transcription module using OpenAI Whisper API or compatible services."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Optional

import httpx
from openai import AsyncOpenAI

from src.utils.logger import get_logger
from src.utils.retry import async_retry

logger = get_logger("audio_transcriber")

SUPPORTED_FORMATS = {"mp3", "m4a", "wav", "ogg", "flac", "webm", "mp4", "mpeg", "mpga"}
# Whisper API max file size is 25MB; chunk larger files
WHISPER_MAX_SIZE = 25 * 1024 * 1024


class AudioTranscriber:
    """Transcribes podcast audio to text using OpenAI Whisper API."""

    def __init__(
        self,
        api_key: str,
        api_base: str = "https://api.openai.com/v1",
        model: str = "whisper-1",
        temp_dir: str = "./temp/audio",
        language: str = "zh",
    ):
        self.client = AsyncOpenAI(api_key=api_key, base_url=api_base)
        self.model = model
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.language = language

    @async_retry(
        max_attempts=3,
        backoff_base=2,
        retryable_exceptions=(httpx.HTTPError, Exception),
    )
    async def download_audio(
        self, audio_url: str, filename: str | None = None
    ) -> Path:
        """Download audio file from URL.

        Args:
            audio_url: URL of the audio file.
            filename: Optional filename. Auto-generated if not provided.

        Returns:
            Path to the downloaded audio file.
        """
        if not filename:
            # Extract filename from URL
            url_path = audio_url.split("?")[0].split("/")[-1]
            if not url_path or "." not in url_path:
                url_path = "audio.mp3"
            filename = url_path

        filepath = self.temp_dir / filename
        logger.info("Downloading audio from %s", audio_url)

        async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
            async with client.stream("GET", audio_url) as response:
                response.raise_for_status()
                with open(filepath, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        f.write(chunk)

        file_size = filepath.stat().st_size
        logger.info(
            "Downloaded audio file: %s (%.1f MB)",
            filepath.name,
            file_size / (1024 * 1024),
        )
        return filepath

    async def transcribe(
        self,
        audio_path: str | Path,
        language: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe an audio file to text.

        Args:
            audio_path: Path to the audio file.
            language: Language code (e.g., 'zh' for Chinese).

        Returns:
            TranscriptionResult with text and metadata.
        """
        audio_path = Path(audio_path)
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        lang = language or self.language
        file_size = audio_path.stat().st_size
        start_time = time.time()

        logger.info(
            "Starting transcription: %s (%.1f MB)",
            audio_path.name,
            file_size / (1024 * 1024),
        )

        if file_size <= WHISPER_MAX_SIZE:
            text = await self._transcribe_file(audio_path, lang)
        else:
            text = await self._transcribe_chunked(audio_path, lang)

        elapsed = time.time() - start_time
        logger.info(
            "Transcription completed in %.1fs: %d characters",
            elapsed,
            len(text),
        )

        return TranscriptionResult(
            text=text,
            language=lang,
            duration_seconds=elapsed,
            file_path=str(audio_path),
        )

    @async_retry(max_attempts=3, backoff_base=2)
    async def _transcribe_file(self, audio_path: Path, language: str) -> str:
        """Transcribe a single audio file (under 25MB)."""
        with open(audio_path, "rb") as audio_file:
            transcript = await self.client.audio.transcriptions.create(
                model=self.model,
                file=audio_file,
                language=language,
                response_format="text",
            )
        return str(transcript).strip()

    async def _transcribe_chunked(self, audio_path: Path, language: str) -> str:
        """Transcribe a large audio file by splitting into chunks.

        For files exceeding the Whisper API size limit, we rely on
        ffmpeg to split the audio into smaller segments.
        """
        import asyncio
        import subprocess

        chunk_dir = self.temp_dir / f"chunks_{audio_path.stem}"
        chunk_dir.mkdir(exist_ok=True)

        # Split audio into 20-minute chunks using ffmpeg
        chunk_duration = 1200  # 20 minutes in seconds
        cmd = [
            "ffmpeg", "-i", str(audio_path),
            "-f", "segment",
            "-segment_time", str(chunk_duration),
            "-c", "copy",
            "-y",
            str(chunk_dir / "chunk_%03d" + audio_path.suffix),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.error("ffmpeg split error: %s", stderr.decode())
                raise RuntimeError(f"ffmpeg failed: {stderr.decode()}")
        except FileNotFoundError:
            logger.warning("ffmpeg not found, attempting single-file transcription")
            return await self._transcribe_file(audio_path, language)

        # Transcribe each chunk
        chunk_files = sorted(chunk_dir.glob(f"chunk_*{audio_path.suffix}"))
        texts = []
        for chunk_file in chunk_files:
            logger.info("Transcribing chunk: %s", chunk_file.name)
            text = await self._transcribe_file(chunk_file, language)
            texts.append(text)

        # Cleanup chunks
        for chunk_file in chunk_files:
            chunk_file.unlink(missing_ok=True)
        chunk_dir.rmdir()

        return "\n".join(texts)

    async def transcribe_from_url(
        self,
        audio_url: str,
        language: str | None = None,
    ) -> TranscriptionResult:
        """Download and transcribe audio from a URL.

        Args:
            audio_url: URL of the audio file.
            language: Language code.

        Returns:
            TranscriptionResult with text and metadata.
        """
        audio_path = await self.download_audio(audio_url)
        try:
            result = await self.transcribe(audio_path, language)
        finally:
            # Clean up downloaded file
            audio_path.unlink(missing_ok=True)
        return result

    def cleanup(self):
        """Remove all temporary files."""
        if self.temp_dir.exists():
            for f in self.temp_dir.iterdir():
                if f.is_file():
                    f.unlink()
            logger.info("Cleaned up temporary audio files")


class TranscriptionResult:
    """Result of an audio transcription."""

    def __init__(
        self,
        text: str,
        language: str = "",
        duration_seconds: float = 0.0,
        file_path: str = "",
    ):
        self.text = text
        self.language = language
        self.duration_seconds = duration_seconds
        self.file_path = file_path

    @property
    def word_count(self) -> int:
        return len(self.text)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "language": self.language,
            "duration_seconds": self.duration_seconds,
            "word_count": self.word_count,
        }
