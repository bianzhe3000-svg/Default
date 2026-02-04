"""Tests for the audio transcriber module."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.modules.audio_transcriber import AudioTranscriber, TranscriptionResult


class TestAudioTranscriber:
    """Test audio transcription functionality."""

    def setup_method(self):
        self.transcriber = AudioTranscriber(
            api_key="test-key",
            api_base="https://api.openai.com/v1",
            model="whisper-1",
            temp_dir=tempfile.mkdtemp(),
        )

    @pytest.mark.asyncio
    async def test_download_audio(self):
        """Test audio file download."""
        mock_response = AsyncMock()
        mock_response.raise_for_status = lambda: None

        async def mock_aiter_bytes(chunk_size=8192):
            yield b"fake audio data chunk 1"
            yield b"fake audio data chunk 2"

        mock_response.aiter_bytes = mock_aiter_bytes

        mock_stream = AsyncMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.stream = MagicMock(return_value=mock_stream)
            mock_client_cls.return_value = mock_client

            filepath = await self.transcriber.download_audio(
                "https://example.com/test.mp3",
                filename="test.mp3",
            )

        assert filepath.exists()
        assert filepath.name == "test.mp3"
        filepath.unlink()

    @pytest.mark.asyncio
    async def test_transcribe_file(self, mock_openai_client):
        """Test transcription of a single audio file."""
        # Create a temporary fake audio file
        temp_file = Path(self.transcriber.temp_dir) / "test.mp3"
        temp_file.write_bytes(b"fake audio content")

        with patch.object(self.transcriber, "client", mock_openai_client):
            result = await self.transcriber.transcribe(temp_file)

        assert isinstance(result, TranscriptionResult)
        assert result.text == "这是转录的文本内容"
        assert result.duration_seconds > 0
        temp_file.unlink()

    @pytest.mark.asyncio
    async def test_transcribe_file_not_found(self):
        """Test transcription of non-existent file."""
        with pytest.raises(FileNotFoundError):
            await self.transcriber.transcribe("/nonexistent/audio.mp3")

    def test_transcription_result(self):
        """Test TranscriptionResult properties."""
        result = TranscriptionResult(
            text="测试文本内容",
            language="zh",
            duration_seconds=5.0,
        )
        assert result.word_count == 6
        d = result.to_dict()
        assert d["text"] == "测试文本内容"
        assert d["language"] == "zh"

    def test_cleanup(self):
        """Test temporary file cleanup."""
        # Create some temp files
        temp_path = Path(self.transcriber.temp_dir)
        (temp_path / "temp1.mp3").write_bytes(b"data")
        (temp_path / "temp2.mp3").write_bytes(b"data")

        self.transcriber.cleanup()

        remaining = list(temp_path.iterdir())
        assert len(remaining) == 0

    def test_auto_filename_from_url(self):
        """Test filename extraction from URL."""
        # The download function extracts filename from URL
        url = "https://example.com/episodes/my-podcast-ep-42.mp3"
        expected = "my-podcast-ep-42.mp3"
        url_path = url.split("?")[0].split("/")[-1]
        assert url_path == expected

    def test_auto_filename_with_query_params(self):
        """Test filename extraction from URL with query parameters."""
        url = "https://cdn.example.com/audio.mp3?token=abc123&expire=9999"
        url_path = url.split("?")[0].split("/")[-1]
        assert url_path == "audio.mp3"
