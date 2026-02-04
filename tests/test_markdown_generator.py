"""Tests for the Markdown document generator."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.modules.content_analyzer import AnalysisResult, KnowledgePoint, Opinion
from src.modules.markdown_generator import MarkdownGenerator


class TestMarkdownGenerator:
    """Test Markdown document generation."""

    def setup_method(self, temp_dir=None):
        self.generator = None

    def _make_generator(self, temp_dir):
        return MarkdownGenerator(summaries_dir=temp_dir)

    def _make_analysis(self) -> AnalysisResult:
        return AnalysisResult(
            summary="这是一个关于AI在教育领域应用的播客摘要。"
            "内容涵盖了个性化学习、智能辅导和教育评估等多个方面。",
            key_points=[
                "AI推动个性化学习发展",
                "智能辅导系统可以24小时在线服务",
                "数据隐私是需要关注的重要问题",
            ],
            main_opinions=[
                Opinion(
                    title="AI将革新教育方式",
                    summary="AI技术正在带来教育领域的革命性变化",
                    details="通过分析学习数据，AI能够为每个学生定制个性化学习计划...",
                    supporting_quotes=["AI技术正在带来革命性的变化"],
                ),
                Opinion(
                    title="数据隐私需要重视",
                    summary="在使用AI教育工具时，学生数据安全至关重要",
                    details="我们需要确保学生的个人信息得到充分保护...",
                    supporting_quotes=["数据隐私是一个重要的问题"],
                ),
            ],
            knowledge_points=[
                KnowledgePoint(
                    topic="教育技术",
                    concept="自适应学习",
                    explanation="根据学生掌握程度自动调整教学内容的难度和进度",
                    category="AI教育",
                ),
                KnowledgePoint(
                    topic="教育技术",
                    concept="自然语言处理",
                    explanation="用于自动批改作文并提供详细反馈的技术",
                    category="AI教育",
                ),
            ],
        )

    def test_generate_markdown(self, temp_dir):
        """Test Markdown content generation."""
        gen = self._make_generator(temp_dir)
        analysis = self._make_analysis()

        content = gen.generate(
            podcast_name="AI教育播客",
            episode_title="第10期：AI与个性化学习",
            analysis=analysis,
            published_at=datetime(2024, 1, 15, tzinfo=timezone.utc),
            duration_seconds=5400,
        )

        assert "# AI教育播客" in content
        assert "第10期：AI与个性化学习" in content
        assert "2024-01-15" in content
        assert "内容总结" in content
        assert "核心要点" in content
        assert "主要观点" in content
        assert "知识点分析学习" in content
        assert "AI推动个性化学习" in content
        assert "自适应学习" in content

    def test_generate_and_save(self, temp_dir):
        """Test generating and saving Markdown document."""
        gen = self._make_generator(temp_dir)
        analysis = self._make_analysis()

        content, filepath = gen.generate_and_save(
            podcast_name="测试播客",
            episode_title="测试剧集",
            analysis=analysis,
            published_at=datetime(2024, 3, 1, tzinfo=timezone.utc),
        )

        assert filepath.exists()
        assert filepath.name == "2024-03-01-podcast-summary.md"
        assert filepath.parent.name == "测试播客"

        saved_content = filepath.read_text(encoding="utf-8")
        assert saved_content == content

    def test_save_duplicate_handling(self, temp_dir):
        """Test that duplicate filenames are handled correctly."""
        gen = self._make_generator(temp_dir)
        analysis = self._make_analysis()
        pub_date = datetime(2024, 3, 1, tzinfo=timezone.utc)

        _, path1 = gen.generate_and_save(
            "测试播客", "剧集1", analysis, pub_date
        )
        _, path2 = gen.generate_and_save(
            "测试播客", "剧集2", analysis, pub_date
        )

        assert path1 != path2
        assert path1.name == "2024-03-01-podcast-summary.md"
        assert "2024-03-01-podcast-summary-1.md" == path2.name

    def test_list_documents(self, temp_dir):
        """Test listing generated documents."""
        gen = self._make_generator(temp_dir)
        analysis = self._make_analysis()

        gen.generate_and_save(
            "播客A", "剧集1", analysis,
            datetime(2024, 1, 1, tzinfo=timezone.utc)
        )
        gen.generate_and_save(
            "播客B", "剧集2", analysis,
            datetime(2024, 2, 1, tzinfo=timezone.utc)
        )

        # List all
        docs = gen.list_documents()
        assert len(docs) == 2

        # List filtered
        docs_a = gen.list_documents("播客A")
        assert len(docs_a) == 1
        assert docs_a[0]["podcast_name"] == "播客A"

    def test_read_document(self, temp_dir):
        """Test reading a generated document."""
        gen = self._make_generator(temp_dir)
        analysis = self._make_analysis()

        content, filepath = gen.generate_and_save(
            "测试播客", "测试剧集", analysis
        )

        read_content = gen.read_document(str(filepath))
        assert read_content == content

    def test_read_document_not_found(self, temp_dir):
        """Test reading a non-existent document."""
        gen = self._make_generator(temp_dir)
        with pytest.raises(FileNotFoundError):
            gen.read_document("/nonexistent/path.md")

    def test_export_to_html(self, temp_dir):
        """Test Markdown to HTML conversion."""
        gen = self._make_generator(temp_dir)
        md_content = "# Title\n\nParagraph text.\n\n- Item 1\n- Item 2"
        html = gen.export_to_html(md_content)

        assert "Title" in html
        assert "Item 1" in html

    def test_format_duration(self):
        """Test duration formatting."""
        assert MarkdownGenerator._format_duration(0) == "未知"
        assert MarkdownGenerator._format_duration(30) == "30秒"
        assert "分钟" in MarkdownGenerator._format_duration(600)
        assert "小时" in MarkdownGenerator._format_duration(3600)
        assert "小时" in MarkdownGenerator._format_duration(5400)

    def test_sanitize_filename(self):
        """Test filename sanitization."""
        assert MarkdownGenerator._sanitize_filename("normal name") == "normal name"
        assert MarkdownGenerator._sanitize_filename("has/slashes") == "has_slashes"
        assert MarkdownGenerator._sanitize_filename("has:colons") == "has_colons"
        assert MarkdownGenerator._sanitize_filename('a<b>c"d') == "a_b_c_d"
