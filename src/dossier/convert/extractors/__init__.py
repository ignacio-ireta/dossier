"""Extractor registry: file suffix -> extractor."""

from __future__ import annotations

from pathlib import Path

from dossier.convert.extractors.base import ExtractContext, ExtractionError, Extractor
from dossier.convert.extractors.mail import EmlExtractor, MsgExtractor
from dossier.convert.extractors.office import DocxExtractor, PptxExtractor
from dossier.convert.extractors.pdf import ImageExtractor, PdfExtractor
from dossier.convert.extractors.sheets import CsvExtractor, XlsxExtractor
from dossier.convert.extractors.web import HtmlExtractor, JsonExtractor, TextExtractor

EXTRACTORS: tuple[Extractor, ...] = (
    DocxExtractor(),
    PptxExtractor(),
    XlsxExtractor(),
    CsvExtractor(),
    PdfExtractor(),
    ImageExtractor(),
    HtmlExtractor(),
    TextExtractor(),
    JsonExtractor(),
    EmlExtractor(),
    MsgExtractor(),
)

BY_SUFFIX: dict[str, Extractor] = {
    suffix: extractor for extractor in EXTRACTORS for suffix in extractor.suffixes
}

# Recordings found in source folders go to the meetings pipeline, not here.
MEDIA_SUFFIXES = frozenset(
    {
        ".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus", ".wma",
        ".mp4", ".mov", ".mkv", ".m4v", ".webm", ".avi",
    }
)  # fmt: skip


def extractor_for(path: Path) -> Extractor | None:
    return BY_SUFFIX.get(path.suffix.lower())


def extractor_id(extractor: Extractor) -> str:
    return f"{extractor.name}@{extractor.version}"


__all__ = [
    "BY_SUFFIX",
    "EXTRACTORS",
    "MEDIA_SUFFIXES",
    "ExtractContext",
    "ExtractionError",
    "Extractor",
    "extractor_for",
    "extractor_id",
]
