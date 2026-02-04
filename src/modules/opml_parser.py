"""OPML file parser for importing podcast RSS feeds."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from src.utils.logger import get_logger

logger = get_logger("opml_parser")


@dataclass
class OPMLEntry:
    """A single podcast entry from an OPML file."""

    name: str
    rss_url: str
    html_url: str = ""
    description: str = ""
    category: str = ""


class OPMLParser:
    """Parses OPML files to extract podcast RSS feed URLs."""

    def parse_file(self, file_path: str) -> list[OPMLEntry]:
        """Parse an OPML file and extract podcast entries.

        Args:
            file_path: Path to the OPML file.

        Returns:
            List of OPMLEntry objects.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"OPML file not found: {file_path}")

        with open(path, "rb") as f:
            content = f.read()

        return self._parse_content(content)

    def parse_string(self, content: str) -> list[OPMLEntry]:
        """Parse OPML content from a string.

        Args:
            content: OPML XML string.

        Returns:
            List of OPMLEntry objects.
        """
        return self._parse_content(content.encode("utf-8"))

    def _parse_content(self, content: bytes) -> list[OPMLEntry]:
        """Parse OPML XML content."""
        entries = []

        try:
            tree = etree.fromstring(content)
        except etree.XMLSyntaxError as exc:
            logger.error("Failed to parse OPML XML: %s", exc)
            raise ValueError(f"Invalid OPML XML: {exc}") from exc

        # Find all outline elements
        body = tree.find("body")
        if body is None:
            logger.warning("OPML has no body element")
            return entries

        self._extract_outlines(body, entries, category="")
        logger.info("Parsed %d podcast entries from OPML", len(entries))
        return entries

    def _extract_outlines(
        self,
        element: etree._Element,
        entries: list[OPMLEntry],
        category: str,
    ):
        """Recursively extract outline elements from OPML."""
        for outline in element.findall("outline"):
            xml_url = outline.get("xmlUrl", "")
            text = outline.get("text", "") or outline.get("title", "")
            outline_type = outline.get("type", "")

            if xml_url:
                # This is a feed entry
                entries.append(
                    OPMLEntry(
                        name=text,
                        rss_url=xml_url,
                        html_url=outline.get("htmlUrl", ""),
                        description=outline.get("description", ""),
                        category=category,
                    )
                )
            elif text and not xml_url:
                # This is a category/folder, recurse into children
                self._extract_outlines(outline, entries, category=text)

    def export_opml(self, entries: list[OPMLEntry], title: str = "Podcast Feeds") -> str:
        """Export podcast entries to OPML format.

        Args:
            entries: List of OPMLEntry objects.
            title: Title for the OPML document.

        Returns:
            OPML XML string.
        """
        root = etree.Element("opml", version="2.0")

        head = etree.SubElement(root, "head")
        title_elem = etree.SubElement(head, "title")
        title_elem.text = title

        body = etree.SubElement(root, "body")

        # Group by category
        categories: dict[str, list[OPMLEntry]] = {}
        for entry in entries:
            cat = entry.category or "Uncategorized"
            categories.setdefault(cat, []).append(entry)

        for cat_name, cat_entries in categories.items():
            if cat_name != "Uncategorized" or len(categories) > 1:
                cat_outline = etree.SubElement(
                    body, "outline", text=cat_name, title=cat_name
                )
                parent = cat_outline
            else:
                parent = body

            for entry in cat_entries:
                attrs = {
                    "type": "rss",
                    "text": entry.name,
                    "title": entry.name,
                    "xmlUrl": entry.rss_url,
                }
                if entry.html_url:
                    attrs["htmlUrl"] = entry.html_url
                if entry.description:
                    attrs["description"] = entry.description
                etree.SubElement(parent, "outline", **attrs)

        return etree.tostring(
            root, pretty_print=True, xml_declaration=True, encoding="UTF-8"
        ).decode("utf-8")
