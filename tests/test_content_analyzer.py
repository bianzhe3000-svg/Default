"""Tests for the content analyzer module."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.modules.content_analyzer import (
    AnalysisResult,
    ContentAnalyzer,
    KnowledgePoint,
    Opinion,
)


class TestContentAnalyzer:
    """Test content analysis functionality."""

    def setup_method(self):
        self.analyzer = ContentAnalyzer(
            api_key="test-key",
            model="gpt-4",
        )

    def test_parse_json_list_valid(self):
        """Test parsing a valid JSON list."""
        content = '["point 1", "point 2", "point 3"]'
        result = ContentAnalyzer._parse_json_list(content)
        assert result == ["point 1", "point 2", "point 3"]

    def test_parse_json_list_with_code_block(self):
        """Test parsing JSON wrapped in markdown code block."""
        content = '```json\n["point 1", "point 2"]\n```'
        result = ContentAnalyzer._parse_json_list(content)
        assert result == ["point 1", "point 2"]

    def test_parse_json_list_fallback(self):
        """Test fallback parsing for non-JSON list."""
        content = "- point 1\n- point 2\n- point 3"
        result = ContentAnalyzer._parse_json_list(content)
        assert len(result) == 3
        assert "point 1" in result[0]

    def test_extract_json_valid(self):
        """Test extracting valid JSON."""
        content = '[{"key": "value"}]'
        result = ContentAnalyzer._extract_json(content)
        assert isinstance(result, list)
        assert result[0]["key"] == "value"

    def test_extract_json_with_surrounding_text(self):
        """Test extracting JSON embedded in text."""
        content = 'Here is the result:\n[{"key": "value"}]\nEnd of result.'
        result = ContentAnalyzer._extract_json(content)
        assert isinstance(result, list)

    def test_extract_json_invalid(self):
        """Test extracting invalid JSON raises error."""
        with pytest.raises(ValueError):
            ContentAnalyzer._extract_json("not json at all")

    @pytest.mark.asyncio
    async def test_generate_summary(self, sample_transcript):
        """Test summary generation with mock LLM."""
        mock_message = MagicMock()
        mock_message.content = "这是一个关于AI在教育领域应用的播客摘要。"
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]

        with patch.object(
            self.analyzer.client.chat.completions,
            "create",
            new_callable=AsyncMock,
            return_value=mock_completion,
        ):
            result = await self.analyzer._generate_summary(sample_transcript)

        assert "AI" in result or "教育" in result or "摘要" in result

    @pytest.mark.asyncio
    async def test_extract_key_points(self, sample_transcript):
        """Test key points extraction with mock LLM."""
        mock_message = MagicMock()
        mock_message.content = json.dumps(
            ["个性化学习", "智能辅导系统", "教育评估", "数据隐私"],
            ensure_ascii=False,
        )
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]

        with patch.object(
            self.analyzer.client.chat.completions,
            "create",
            new_callable=AsyncMock,
            return_value=mock_completion,
        ):
            result = await self.analyzer._extract_key_points(sample_transcript)

        assert isinstance(result, list)
        assert len(result) >= 3

    @pytest.mark.asyncio
    async def test_extract_opinions(self, sample_transcript):
        """Test opinion extraction with mock LLM."""
        opinions_data = [
            {
                "title": "AI推动个性化教育",
                "summary": "AI可以为每个学生定制学习计划",
                "details": "通过分析学习数据...",
                "supporting_quotes": ["通过分析学生的学习数据"],
            }
        ]
        mock_message = MagicMock()
        mock_message.content = json.dumps(opinions_data, ensure_ascii=False)
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]

        with patch.object(
            self.analyzer.client.chat.completions,
            "create",
            new_callable=AsyncMock,
            return_value=mock_completion,
        ):
            result = await self.analyzer._extract_opinions(sample_transcript)

        assert len(result) == 1
        assert isinstance(result[0], Opinion)
        assert result[0].title == "AI推动个性化教育"

    @pytest.mark.asyncio
    async def test_extract_knowledge_points(self, sample_transcript):
        """Test knowledge point extraction with mock LLM."""
        kp_data = [
            {
                "topic": "人工智能",
                "concept": "自适应学习",
                "explanation": "根据学生水平自动调整教学内容",
                "category": "教育技术",
            }
        ]
        mock_message = MagicMock()
        mock_message.content = json.dumps(kp_data, ensure_ascii=False)
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]

        with patch.object(
            self.analyzer.client.chat.completions,
            "create",
            new_callable=AsyncMock,
            return_value=mock_completion,
        ):
            result = await self.analyzer._extract_knowledge_points(sample_transcript)

        assert len(result) == 1
        assert isinstance(result[0], KnowledgePoint)
        assert result[0].concept == "自适应学习"

    def test_analysis_result_serialization(self):
        """Test AnalysisResult serialization methods."""
        result = AnalysisResult(
            summary="Test summary",
            key_points=["point1", "point2"],
            main_opinions=[
                Opinion(
                    title="Opinion 1",
                    summary="Summary",
                    details="Details",
                    supporting_quotes=["quote"],
                )
            ],
            knowledge_points=[
                KnowledgePoint(
                    topic="Topic",
                    concept="Concept",
                    explanation="Explanation",
                    category="Category",
                )
            ],
        )

        d = result.to_dict()
        assert d["summary"] == "Test summary"
        assert len(d["key_points"]) == 2
        assert len(d["main_opinions"]) == 1
        assert len(d["knowledge_points"]) == 1

        # Test JSON serialization
        kp_json = result.key_points_json()
        assert "point1" in kp_json

        mo_json = result.main_opinions_json()
        assert "Opinion 1" in mo_json

        knp_json = result.knowledge_points_json()
        assert "Concept" in knp_json
