"""Extractor contract and extraction errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from dossier.convert.model import ExtractionResult
from dossier.core.config import OcrMode
from dossier.core.errors import DossierError


class ExtractionError(DossierError):
    """A single file could not be converted. ``code`` names the reason."""

    code = "extraction_failed"

    def __init__(self, message: str, *, code: str | None = None, path: Path | None = None):
        super().__init__(message, path=path)
        if code:
            self.code = code


@dataclass(frozen=True)
class ExtractContext:
    ocr: OcrMode = OcrMode.AUTO
    languages: list[str] = field(default_factory=lambda: ["en"])
    max_sheet_rows: int = 50_000
    preview_rows: int = 20

    @property
    def ocr_languages(self) -> list[str]:
        """BCP-47 tags for OCR engines from the workspace's short language codes."""
        mapping = {
            "en": "en-US",
            "es": "es-ES",
            "pt": "pt-BR",
            "fr": "fr-FR",
            "de": "de-DE",
            "it": "it-IT",
            "nl": "nl-NL",
            "no": "nb-NO",
            "nb": "nb-NO",
        }
        return [mapping.get(code, code) for code in self.languages]


class Extractor(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> int: ...

    @property
    def suffixes(self) -> tuple[str, ...]: ...

    def extract(self, path: Path, ctx: ExtractContext) -> ExtractionResult: ...
