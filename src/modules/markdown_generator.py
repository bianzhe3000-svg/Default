"""Markdown document generator for podcast summaries."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.modules.content_analyzer import AnalysisResult, KnowledgePoint, Opinion
from src.utils.logger import get_logger

logger = get_logger("markdown_generator")

# Default template as a string fallback
DEFAULT_TEMPLATE = """# {{ podcast_name }} - {{ episode_title }}

> **日期**: {{ date }}
> **播客**: {{ podcast_name }}
> **剧集**: {{ episode_title }}
> **时长**: {{ duration }}

---

## 📝 内容总结

{{ summary }}

---

## 🎯 核心要点

{% for point in key_points %}
{{ loop.index }}. {{ point }}
{% endfor %}

---

## 💡 主要观点

{% for opinion in main_opinions %}
### {{ loop.index }}. {{ opinion.title }}

**摘要**: {{ opinion.summary }}

<details>
<summary>点击展开详细内容</summary>

{{ opinion.details }}

{% if opinion.supporting_quotes %}
**相关引用:**

{% for quote in opinion.supporting_quotes %}
> {{ quote }}

{% endfor %}
{% endif %}

</details>

{% endfor %}

---

## 📚 知识点分析学习

{% for category, points in knowledge_by_category.items() %}
### {{ category }}

{% for kp in points %}
#### {{ kp.concept }}

{{ kp.explanation }}

{% endfor %}
{% endfor %}

---

*本文档由播客自动化处理系统自动生成*
*生成时间: {{ generated_at }}*
"""


class MarkdownGenerator:
    """Generates standardized Markdown documents from analysis results."""

    def __init__(
        self,
        template_dir: str = "templates",
        summaries_dir: str = "./summaries",
    ):
        self.summaries_dir = Path(summaries_dir)
        self.summaries_dir.mkdir(parents=True, exist_ok=True)
        self.template_dir = Path(template_dir)

        # Set up Jinja2 environment
        if self.template_dir.exists():
            self.env = Environment(
                loader=FileSystemLoader(str(self.template_dir)),
                autoescape=select_autoescape([]),
                trim_blocks=True,
                lstrip_blocks=True,
            )
        else:
            self.env = None

    def generate(
        self,
        podcast_name: str,
        episode_title: str,
        analysis: AnalysisResult,
        published_at: Optional[datetime] = None,
        duration_seconds: int = 0,
        audio_url: str = "",
    ) -> str:
        """Generate a Markdown document from analysis results.

        Args:
            podcast_name: Name of the podcast.
            episode_title: Title of the episode.
            analysis: AnalysisResult from ContentAnalyzer.
            published_at: Episode publication date.
            duration_seconds: Duration of the episode in seconds.
            audio_url: URL of the audio file.

        Returns:
            Generated Markdown content as a string.
        """
        date_str = (
            published_at.strftime("%Y-%m-%d") if published_at else datetime.now().strftime("%Y-%m-%d")
        )

        # Format duration
        duration = self._format_duration(duration_seconds)

        # Group knowledge points by category
        knowledge_by_category: dict[str, list[KnowledgePoint]] = {}
        for kp in analysis.knowledge_points:
            cat = kp.category or kp.topic or "其他"
            knowledge_by_category.setdefault(cat, []).append(kp)

        template_vars = {
            "podcast_name": podcast_name,
            "episode_title": episode_title,
            "date": date_str,
            "duration": duration,
            "audio_url": audio_url,
            "summary": analysis.summary,
            "key_points": analysis.key_points,
            "main_opinions": analysis.main_opinions,
            "knowledge_by_category": knowledge_by_category,
            "knowledge_points": analysis.knowledge_points,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        # Try to use file template first, then fall back to default
        try:
            if self.env:
                template = self.env.get_template("podcast_summary.md.j2")
                content = template.render(**template_vars)
            else:
                raise FileNotFoundError("No template directory")
        except Exception:
            # Use inline default template
            from jinja2 import Template

            template = Template(DEFAULT_TEMPLATE)
            content = template.render(**template_vars)

        return content

    def save(
        self,
        content: str,
        podcast_name: str,
        published_at: Optional[datetime] = None,
    ) -> Path:
        """Save generated Markdown to the file system.

        Args:
            content: Markdown content string.
            podcast_name: Podcast name for directory organization.
            published_at: Episode publication date for filename.

        Returns:
            Path to the saved Markdown file.
        """
        date_str = (
            published_at.strftime("%Y-%m-%d") if published_at else datetime.now().strftime("%Y-%m-%d")
        )

        # Create podcast directory
        safe_name = self._sanitize_filename(podcast_name)
        podcast_dir = self.summaries_dir / safe_name
        podcast_dir.mkdir(parents=True, exist_ok=True)

        # Generate filename
        filename = f"{date_str}-podcast-summary.md"
        filepath = podcast_dir / filename

        # Handle duplicate filenames
        counter = 1
        while filepath.exists():
            filename = f"{date_str}-podcast-summary-{counter}.md"
            filepath = podcast_dir / filename
            counter += 1

        filepath.write_text(content, encoding="utf-8")
        logger.info("Saved Markdown document: %s", filepath)
        return filepath

    def generate_and_save(
        self,
        podcast_name: str,
        episode_title: str,
        analysis: AnalysisResult,
        published_at: Optional[datetime] = None,
        duration_seconds: int = 0,
        audio_url: str = "",
    ) -> tuple[str, Path]:
        """Generate and save a Markdown document.

        Returns:
            Tuple of (markdown_content, file_path).
        """
        content = self.generate(
            podcast_name=podcast_name,
            episode_title=episode_title,
            analysis=analysis,
            published_at=published_at,
            duration_seconds=duration_seconds,
            audio_url=audio_url,
        )
        filepath = self.save(content, podcast_name, published_at)
        return content, filepath

    def list_documents(
        self, podcast_name: Optional[str] = None
    ) -> list[dict]:
        """List generated documents, optionally filtered by podcast name.

        Returns:
            List of document info dicts with path, name, date, and size.
        """
        documents = []
        search_dir = self.summaries_dir
        if podcast_name:
            safe_name = self._sanitize_filename(podcast_name)
            search_dir = self.summaries_dir / safe_name

        if not search_dir.exists():
            return documents

        for md_file in search_dir.rglob("*.md"):
            stat = md_file.stat()
            documents.append(
                {
                    "path": str(md_file),
                    "relative_path": str(md_file.relative_to(self.summaries_dir)),
                    "filename": md_file.name,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "podcast_name": md_file.parent.name,
                }
            )

        documents.sort(key=lambda d: d["modified_at"], reverse=True)
        return documents

    def read_document(self, file_path: str) -> str:
        """Read a Markdown document.

        Args:
            file_path: Path to the Markdown file.

        Returns:
            Markdown content as string.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Document not found: {file_path}")
        return path.read_text(encoding="utf-8")

    def export_to_html(self, markdown_content: str) -> str:
        """Convert Markdown to HTML for preview."""
        import markdown as md

        return md.markdown(
            markdown_content,
            extensions=["tables", "fenced_code", "toc"],
        )

    @staticmethod
    def _format_duration(seconds: int) -> str:
        """Format seconds to HH:MM:SS string."""
        if not seconds:
            return "未知"
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        if hours:
            return f"{hours}小时{minutes}分钟{secs}秒"
        elif minutes:
            return f"{minutes}分钟{secs}秒"
        return f"{secs}秒"

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """Sanitize a string for use as a filename/directory name."""
        # Remove or replace characters that are not safe for filenames
        unsafe_chars = '<>:"/\\|?*'
        result = name
        for char in unsafe_chars:
            result = result.replace(char, "_")
        return result.strip().strip(".")
