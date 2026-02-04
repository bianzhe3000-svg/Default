"""Tests for the OPML parser module."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.modules.opml_parser import OPMLEntry, OPMLParser


class TestOPMLParser:
    """Test OPML file parsing and export."""

    def setup_method(self):
        self.parser = OPMLParser()

    def test_parse_string(self, sample_opml_content):
        """Test parsing OPML from a string."""
        entries = self.parser.parse_string(sample_opml_content)

        assert len(entries) == 3
        assert entries[0].name == "测试播客1"
        assert entries[0].rss_url == "https://example.com/feed1.xml"
        assert entries[0].html_url == "https://example.com/podcast1"
        assert entries[0].category == "技术"

        assert entries[1].name == "测试播客2"
        assert entries[1].category == "技术"

        assert entries[2].name == "测试播客3"
        assert entries[2].category == ""

    def test_parse_file(self, sample_opml_content, temp_dir):
        """Test parsing OPML from a file."""
        filepath = Path(temp_dir) / "test.opml"
        filepath.write_text(sample_opml_content, encoding="utf-8")

        entries = self.parser.parse_file(str(filepath))
        assert len(entries) == 3

    def test_parse_file_not_found(self):
        """Test parsing a non-existent file."""
        with pytest.raises(FileNotFoundError):
            self.parser.parse_file("/nonexistent/path.opml")

    def test_parse_invalid_xml(self):
        """Test parsing invalid XML content."""
        with pytest.raises(ValueError, match="Invalid OPML XML"):
            self.parser.parse_string("not valid xml <><><<>")

    def test_parse_empty_opml(self):
        """Test parsing OPML with no outlines."""
        content = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>Empty</title></head>
  <body></body>
</opml>"""
        entries = self.parser.parse_string(content)
        assert len(entries) == 0

    def test_parse_opml_no_body(self):
        """Test parsing OPML without body element."""
        content = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>No Body</title></head>
</opml>"""
        entries = self.parser.parse_string(content)
        assert len(entries) == 0

    def test_export_opml(self):
        """Test exporting entries to OPML format."""
        entries = [
            OPMLEntry(
                name="Podcast 1",
                rss_url="https://example.com/feed1.xml",
                html_url="https://example.com/1",
                category="Tech",
            ),
            OPMLEntry(
                name="Podcast 2",
                rss_url="https://example.com/feed2.xml",
                category="Tech",
            ),
            OPMLEntry(
                name="Podcast 3",
                rss_url="https://example.com/feed3.xml",
                category="News",
            ),
        ]

        opml_str = self.parser.export_opml(entries, title="My Feeds")
        assert "My Feeds" in opml_str
        assert "feed1.xml" in opml_str
        assert "feed2.xml" in opml_str
        assert "feed3.xml" in opml_str
        assert "Tech" in opml_str
        assert "News" in opml_str

    def test_export_roundtrip(self, sample_opml_content):
        """Test that export then parse produces same entries."""
        entries = self.parser.parse_string(sample_opml_content)
        exported = self.parser.export_opml(entries)
        re_entries = self.parser.parse_string(exported)

        assert len(re_entries) == len(entries)
        for orig, reimported in zip(entries, re_entries):
            assert orig.name == reimported.name
            assert orig.rss_url == reimported.rss_url
