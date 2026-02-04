"""Content analysis engine for Chinese podcast transcript processing."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Optional

from openai import AsyncOpenAI

from src.utils.logger import get_logger
from src.utils.retry import async_retry

logger = get_logger("content_analyzer")


@dataclass
class Opinion:
    """A main opinion/argument from the podcast."""

    title: str
    summary: str
    details: str
    supporting_quotes: list[str] = field(default_factory=list)


@dataclass
class KnowledgePoint:
    """A knowledge point extracted from the podcast."""

    topic: str
    concept: str
    explanation: str
    category: str = ""


@dataclass
class AnalysisResult:
    """Complete analysis result for a podcast episode."""

    summary: str = ""
    key_points: list[str] = field(default_factory=list)
    main_opinions: list[Opinion] = field(default_factory=list)
    knowledge_points: list[KnowledgePoint] = field(default_factory=list)
    duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "key_points": self.key_points,
            "main_opinions": [
                {
                    "title": o.title,
                    "summary": o.summary,
                    "details": o.details,
                    "supporting_quotes": o.supporting_quotes,
                }
                for o in self.main_opinions
            ],
            "knowledge_points": [
                {
                    "topic": k.topic,
                    "concept": k.concept,
                    "explanation": k.explanation,
                    "category": k.category,
                }
                for k in self.knowledge_points
            ],
            "duration_seconds": self.duration_seconds,
        }

    def key_points_json(self) -> str:
        return json.dumps(self.key_points, ensure_ascii=False)

    def main_opinions_json(self) -> str:
        return json.dumps(
            [
                {
                    "title": o.title,
                    "summary": o.summary,
                    "details": o.details,
                    "supporting_quotes": o.supporting_quotes,
                }
                for o in self.main_opinions
            ],
            ensure_ascii=False,
        )

    def knowledge_points_json(self) -> str:
        return json.dumps(
            [
                {
                    "topic": k.topic,
                    "concept": k.concept,
                    "explanation": k.explanation,
                    "category": k.category,
                }
                for k in self.knowledge_points
            ],
            ensure_ascii=False,
        )


class ContentAnalyzer:
    """Analyzes podcast transcripts using LLM for Chinese content understanding."""

    def __init__(
        self,
        api_key: str,
        api_base: str = "https://api.openai.com/v1",
        model: str = "gpt-4",
        max_tokens: int = 4096,
        temperature: float = 0.3,
    ):
        self.client = AsyncOpenAI(api_key=api_key, base_url=api_base)
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    async def analyze(
        self,
        transcript: str,
        episode_title: str = "",
        podcast_name: str = "",
    ) -> AnalysisResult:
        """Perform full analysis on a podcast transcript.

        Args:
            transcript: Full transcript text.
            episode_title: Title of the episode.
            podcast_name: Name of the podcast.

        Returns:
            AnalysisResult with summary, key points, opinions, and knowledge points.
        """
        start_time = time.time()
        result = AnalysisResult()

        # Truncate transcript if too long for context window
        max_chars = 60000
        truncated = transcript[:max_chars] if len(transcript) > max_chars else transcript
        if len(transcript) > max_chars:
            logger.warning(
                "Transcript truncated from %d to %d characters",
                len(transcript),
                max_chars,
            )

        context = self._build_context(truncated, episode_title, podcast_name)

        # Run analysis tasks
        summary = await self._generate_summary(context)
        key_points = await self._extract_key_points(context)
        opinions = await self._extract_opinions(context)
        knowledge = await self._extract_knowledge_points(context)

        result.summary = summary
        result.key_points = key_points
        result.main_opinions = opinions
        result.knowledge_points = knowledge
        result.duration_seconds = time.time() - start_time

        logger.info(
            "Analysis completed in %.1fs: %d key points, %d opinions, %d knowledge points",
            result.duration_seconds,
            len(result.key_points),
            len(result.main_opinions),
            len(result.knowledge_points),
        )
        return result

    def _build_context(
        self, transcript: str, episode_title: str, podcast_name: str
    ) -> str:
        """Build context string for LLM prompts."""
        parts = []
        if podcast_name:
            parts.append(f"播客名称: {podcast_name}")
        if episode_title:
            parts.append(f"剧集标题: {episode_title}")
        parts.append(f"\n转录文本:\n{transcript}")
        return "\n".join(parts)

    @async_retry(max_attempts=3, backoff_base=2)
    async def _generate_summary(self, context: str) -> str:
        """Generate a 1000-2000 character summary of the podcast."""
        prompt = """请根据以下播客转录文本，生成一份1000-2000字的精炼概述。

要求：
1. 涵盖播客剧集的核心主题和主要内容
2. 使用清晰、流畅的中文表达
3. 保持客观、准确，忠实于原始内容
4. 按照逻辑顺序组织内容
5. 字数控制在1000-2000字之间

""" + context

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "你是一位专业的中文播客内容分析师，擅长提取和总结播客的核心内容。",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        return response.choices[0].message.content.strip()

    @async_retry(max_attempts=3, backoff_base=2)
    async def _extract_key_points(self, context: str) -> list[str]:
        """Extract 5-8 key points from the podcast."""
        prompt = """请根据以下播客转录文本，提取5-8个核心要点。

要求：
1. 每个要点应简洁明了，概括性强
2. 每个要点控制在50-100字以内
3. 涵盖播客中最重要的信息
4. 使用中文输出

请严格按照以下JSON格式输出：
["要点1", "要点2", "要点3", ...]

""" + context

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "你是一位专业的中文播客内容分析师。请严格按照JSON格式输出。",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=2048,
            temperature=self.temperature,
        )

        content = response.choices[0].message.content.strip()
        return self._parse_json_list(content)

    @async_retry(max_attempts=3, backoff_base=2)
    async def _extract_opinions(self, context: str) -> list[Opinion]:
        """Extract main opinions and arguments from the podcast."""
        prompt = """请根据以下播客转录文本，识别并详细阐述播客中的核心论点。

要求：
1. 识别3-5个核心论点
2. 每个论点包含：
   - title: 论点标题（简短概括）
   - summary: 论点摘要（50-100字）
   - details: 详细内容（200-500字，展开阐述）
   - supporting_quotes: 相关原文引用（1-3条）

请严格按照以下JSON格式输出：
[
  {
    "title": "论点标题",
    "summary": "论点摘要",
    "details": "详细内容...",
    "supporting_quotes": ["引用1", "引用2"]
  }
]

""" + context

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "你是一位专业的中文播客内容分析师。请严格按照JSON格式输出。",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )

        content = response.choices[0].message.content.strip()
        opinions = []
        try:
            data = self._extract_json(content)
            if isinstance(data, list):
                for item in data:
                    opinions.append(
                        Opinion(
                            title=item.get("title", ""),
                            summary=item.get("summary", ""),
                            details=item.get("details", ""),
                            supporting_quotes=item.get("supporting_quotes", []),
                        )
                    )
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error("Failed to parse opinions JSON: %s", exc)
        return opinions

    @async_retry(max_attempts=3, backoff_base=2)
    async def _extract_knowledge_points(self, context: str) -> list[KnowledgePoint]:
        """Extract knowledge points, concepts, and terms from the podcast."""
        prompt = """请根据以下播客转录文本，提取其中提及的专业知识、概念和术语。

要求：
1. 提取5-10个知识点
2. 按主题分类整理
3. 每个知识点包含：
   - topic: 所属主题
   - concept: 概念/术语名称
   - explanation: 解释说明（100-200字）
   - category: 分类标签

请严格按照以下JSON格式输出：
[
  {
    "topic": "主题名称",
    "concept": "概念名称",
    "explanation": "详细解释...",
    "category": "分类"
  }
]

""" + context

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "你是一位专业的中文播客内容分析师。请严格按照JSON格式输出。",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )

        content = response.choices[0].message.content.strip()
        knowledge_points = []
        try:
            data = self._extract_json(content)
            if isinstance(data, list):
                for item in data:
                    knowledge_points.append(
                        KnowledgePoint(
                            topic=item.get("topic", ""),
                            concept=item.get("concept", ""),
                            explanation=item.get("explanation", ""),
                            category=item.get("category", ""),
                        )
                    )
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error("Failed to parse knowledge points JSON: %s", exc)
        return knowledge_points

    @staticmethod
    def _parse_json_list(content: str) -> list[str]:
        """Parse a JSON list from LLM output, handling markdown code blocks."""
        cleaned = content.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(lines[1:-1]) if len(lines) > 2 else cleaned
        try:
            data = json.loads(cleaned)
            if isinstance(data, list):
                return [str(item) for item in data]
        except json.JSONDecodeError:
            pass
        # Fallback: try to extract list items
        items = []
        for line in content.split("\n"):
            line = line.strip().strip("-").strip("*").strip()
            if line and not line.startswith("[") and not line.startswith("]"):
                items.append(line.strip('"').strip("'"))
        return items[:8]

    @staticmethod
    def _extract_json(content: str):
        """Extract JSON from LLM output, handling markdown code blocks."""
        cleaned = content.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(lines[1:-1]) if len(lines) > 2 else cleaned
            cleaned = cleaned.strip()

        # Try direct parse
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Try to find JSON array in the text
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Could not extract JSON from content: {content[:200]}")
