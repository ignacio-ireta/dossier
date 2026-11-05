"""HTML, plain text, Markdown and JSON."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

from dossier.convert.extractors.base import ExtractContext
from dossier.convert.model import (
    Block,
    CodeBlock,
    DocumentIR,
    ExtractionResult,
    ExtractionWarning,
    Heading,
    ListItem,
    Paragraph,
    RawMarkdown,
    Section,
    SectionKind,
    Table,
)

MAX_TEXT_BYTES = 5 * 1024 * 1024
MAX_JSON_CHARS = 200_000


def read_text(path: Path, limit: int = MAX_TEXT_BYTES) -> tuple[str, bool]:
    raw = path.read_bytes()
    truncated = len(raw) > limit
    raw = raw[:limit]
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding), truncated
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), truncated


class _HtmlToBlocks(HTMLParser):
    """Small, dependency-free HTML reader: headings, paragraphs, lists, tables, code."""

    _SKIP = frozenset({"script", "style", "noscript", "template", "svg"})
    _BLOCK = frozenset(
        {"p", "div", "section", "article", "header", "footer", "blockquote", "br", "tr"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.title: str | None = None
        self._text: list[str] = []
        self._skip = 0
        self._heading: int | None = None
        self._lists: list[bool] = []
        self._in_li = False
        self._pre = False
        self._in_title = False
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._text)).strip()
        self._text = []
        if not text:
            return
        if self._heading:
            self.blocks.append(Heading(level=self._heading, text=text))
        elif self._in_li:
            self.blocks.append(
                ListItem(
                    text=text,
                    level=max(0, len(self._lists) - 1),
                    ordered=bool(self._lists and self._lists[-1]),
                )
            )
        else:
            self.blocks.append(Paragraph(text=text))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = True
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._flush()
            self._heading = int(tag[1])
        elif tag in {"ul", "ol"}:
            self._flush()
            self._lists.append(tag == "ol")
        elif tag == "li":
            self._flush()
            self._in_li = True
        elif tag == "pre":
            self._flush()
            self._pre = True
        elif tag == "table":
            self._flush()
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
        elif tag in self._BLOCK and self._cell is None:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = False
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._flush()
            self._heading = None
        elif tag in {"ul", "ol"}:
            self._flush()
            if self._lists:
                self._lists.pop()
        elif tag == "li":
            self._flush()
            self._in_li = False
        elif tag == "pre":
            code = "".join(self._text).strip("\n")
            self._text = []
            if code.strip():
                self.blocks.append(CodeBlock(text=code))
            self._pre = False
        elif tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._table is not None and self._row is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.blocks.append(Table(rows=self._table))
            self._table = None
        elif tag in self._BLOCK and self._cell is None:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title = (self.title or "") + data.strip()
        elif self._cell is not None:
            self._cell.append(data)
        else:
            self._text.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def html_to_blocks(html: str) -> tuple[str | None, list[Block]]:
    parser = _HtmlToBlocks()
    parser.feed(html)
    parser.close()
    return parser.title, parser.blocks


def _truncation_warning(truncated: bool) -> list[ExtractionWarning]:
    if not truncated:
        return []
    return [ExtractionWarning(code="TEXT_TRUNCATED", message="file larger than 5 MB; cut")]


class HtmlExtractor:
    name = "html"
    version = 1
    suffixes = (".html", ".htm", ".xhtml")

    def extract(self, path: Path, ctx: ExtractContext) -> ExtractionResult:
        del ctx
        html, truncated = read_text(path)
        title, blocks = html_to_blocks(html)
        return ExtractionResult(
            document=DocumentIR(
                source_type="html",
                title=title or None,
                sections=[Section(kind=SectionKind.BODY, blocks=blocks)],
            ),
            warnings=_truncation_warning(truncated),
        )


class TextExtractor:
    name = "text"
    version = 1
    suffixes = (".txt", ".md", ".markdown", ".rst", ".log")

    def extract(self, path: Path, ctx: ExtractContext) -> ExtractionResult:
        del ctx
        text, truncated = read_text(path)
        if path.suffix.lower() in {".md", ".markdown"}:
            text = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL)
            blocks: list[Block] = [RawMarkdown(text=text)]
        else:
            blocks = [
                Paragraph(text=chunk)
                for chunk in re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
                if chunk.strip()
            ]
        return ExtractionResult(
            document=DocumentIR(
                source_type=path.suffix.lower().lstrip(".") or "text",
                sections=[Section(kind=SectionKind.BODY, blocks=blocks)],
            ),
            warnings=_truncation_warning(truncated),
        )


class JsonExtractor:
    name = "json"
    version = 1
    suffixes = (".json",)

    def extract(self, path: Path, ctx: ExtractContext) -> ExtractionResult:
        del ctx
        text, truncated = read_text(path)
        warnings = _truncation_warning(truncated)
        try:
            pretty = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            pretty = text
            warnings.append(ExtractionWarning(code="INVALID_JSON", message="kept as text"))
        if len(pretty) > MAX_JSON_CHARS:
            pretty = pretty[:MAX_JSON_CHARS]
            warnings.append(
                ExtractionWarning(code="TEXT_TRUNCATED", message="JSON cut at 200,000 characters")
            )
        return ExtractionResult(
            document=DocumentIR(
                source_type="json",
                sections=[
                    Section(kind=SectionKind.BODY, blocks=[CodeBlock(text=pretty, language="json")])
                ],
            ),
            warnings=warnings,
        )
