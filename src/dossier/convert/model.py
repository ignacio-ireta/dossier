"""Intermediate representation shared by every extractor and the writer.

Extractors never write Markdown: they produce a :class:`DocumentIR`, which the
normalizer cleans and the writer renders, so every format ends up with the same
structure (sections with headings, paragraphs, lists, tables).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class SectionKind(StrEnum):
    BODY = "body"
    SLIDE = "slide"
    NOTES = "notes"
    SHEET = "sheet"
    PAGE = "page"
    MESSAGE = "message"
    IMAGE = "image"


class Heading(BaseModel):
    kind: Literal["heading"] = "heading"
    level: int = Field(ge=1, le=6)
    text: str


class Paragraph(BaseModel):
    kind: Literal["paragraph"] = "paragraph"
    text: str


class ListItem(BaseModel):
    kind: Literal["list_item"] = "list_item"
    text: str
    level: int = Field(default=0, ge=0)
    ordered: bool = False


class Table(BaseModel):
    kind: Literal["table"] = "table"
    rows: list[list[str]]
    has_header: bool = True
    caption: str | None = None
    total_rows: int | None = None  # when the rendered rows are a preview


class ImageRef(BaseModel):
    kind: Literal["image"] = "image"
    name: str | None = None
    alt: str | None = None


class CodeBlock(BaseModel):
    kind: Literal["code"] = "code"
    text: str
    language: str | None = None


class RawMarkdown(BaseModel):
    """Markdown taken verbatim from a Markdown source file."""

    kind: Literal["raw"] = "raw"
    text: str


Block = Annotated[
    Heading | Paragraph | ListItem | Table | ImageRef | CodeBlock | RawMarkdown,
    Field(discriminator="kind"),
]


class Section(BaseModel):
    kind: SectionKind
    index: int | None = None
    title: str | None = None
    blocks: list[Block] = Field(default_factory=list)


class DocumentIR(BaseModel):
    source_type: str
    title: str | None = None
    sections: list[Section] = Field(default_factory=list)
    properties: dict[str, str] = Field(default_factory=dict)


class ExtractionWarning(BaseModel):
    code: str
    message: str
    location: str | None = None


@dataclass
class SheetData:
    """Full tabular data for one sheet, written as CSV next to the Markdown."""

    name: str
    rows: list[list[str]]


@dataclass
class ExtractionResult:
    document: DocumentIR
    warnings: list[ExtractionWarning] = field(default_factory=list)
    sheets: list[SheetData] = field(default_factory=list)
