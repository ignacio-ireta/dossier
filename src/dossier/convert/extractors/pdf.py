"""PDF extraction with pdfplumber (MIT); scanned pages are rendered with pypdfium2
(Apache-2.0/BSD) and recognized through the platform OCR backend.

Per page: tables are detected first; text lines inside a table are excluded from
the text pass, then lines and tables are interleaved top to bottom. Lines are
joined into paragraphs by vertical gap, and a single short line set noticeably
larger than the page's body text becomes a heading (a heuristic).
"""

from __future__ import annotations

import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from dossier.convert import ocr
from dossier.convert.extractors.base import ExtractContext, ExtractionError
from dossier.convert.model import (
    Block,
    DocumentIR,
    ExtractionResult,
    ExtractionWarning,
    Heading,
    Paragraph,
    Section,
    SectionKind,
    Table,
)
from dossier.core.config import OcrMode

_HEADING_RATIOS = ((1.6, 1), (1.3, 2), (1.15, 3))
_OCR_DPI = 220


def _inside(obj: dict[str, Any], boxes: list[tuple[float, float, float, float]]) -> bool:
    cx = (float(obj["x0"]) + float(obj["x1"])) / 2
    cy = (float(obj["top"]) + float(obj["bottom"])) / 2
    return any(x0 <= cx <= x1 and top <= cy <= bottom for x0, top, x1, bottom in boxes)


class PdfExtractor:
    name = "pdf"
    version = 1
    suffixes = (".pdf",)

    def extract(self, path: Path, ctx: ExtractContext) -> ExtractionResult:
        import pdfplumber

        try:
            pdf = pdfplumber.open(str(path))
        except Exception as exc:
            name = type(exc).__name__.lower()
            if "password" in name or "encrypt" in str(exc).lower():
                raise ExtractionError(
                    "PDF is password protected", code="password_protected", path=path
                ) from exc
            raise ExtractionError(
                f"cannot open PDF ({type(exc).__name__})", code="corrupt_file", path=path
            ) from exc
        warnings: list[ExtractionWarning] = []
        sections: list[Section] = []
        scanned: list[int] = []
        try:
            for number, page in enumerate(pdf.pages, start=1):
                if ctx.ocr is OcrMode.FORCE:
                    blocks: list[Block] = []
                    scanned.append(number)
                else:
                    blocks, has_text = self._page_blocks(page, number, warnings)
                    if not has_text and page.images:
                        scanned.append(number)
                sections.append(Section(kind=SectionKind.PAGE, index=number, blocks=blocks))
            metadata = dict(pdf.metadata or {})
            page_count = len(pdf.pages)
        except ExtractionError:
            raise
        except Exception as exc:
            raise ExtractionError(
                f"cannot read PDF ({type(exc).__name__})", code="corrupt_file", path=path
            ) from exc
        finally:
            pdf.close()

        if scanned:
            self._ocr_pages(path, scanned, sections, warnings, ctx)
        if not any(section.blocks for section in sections) and scanned:
            raise ExtractionError(
                "PDF has no text layer and OCR found no text (or is turned off)",
                code="scanned_no_text",
                path=path,
            )
        title = metadata.get("Title")
        return ExtractionResult(
            document=DocumentIR(
                source_type="pdf",
                title=str(title) if isinstance(title, str) and title.strip() else None,
                sections=sections,
                properties={"Pages": str(page_count)},
            ),
            warnings=warnings,
        )

    def _page_blocks(
        self, page: Any, number: int, warnings: list[ExtractionWarning]
    ) -> tuple[list[Block], bool]:
        positioned: list[tuple[float, Block]] = []
        boxes: list[tuple[float, float, float, float]] = []
        try:
            for table in page.find_tables():
                rows = [["" if c is None else str(c) for c in row] for row in table.extract()]
                if any(any(row) for row in rows):
                    boxes.append(tuple(table.bbox))
                    positioned.append((float(table.bbox[1]), Table(rows=rows)))
        except Exception as exc:
            warnings.append(
                ExtractionWarning(
                    code="TABLE_DETECTION_FAILED",
                    message=type(exc).__name__,
                    location=f"page {number}",
                )
            )
        text_page = page.filter(lambda obj: not _inside(obj, boxes)) if boxes else page
        lines = text_page.extract_text_lines(strip=True, return_chars=True)
        body = self._body_size(lines)
        paragraph: list[str] = []
        paragraph_top = 0.0
        previous_bottom: float | None = None
        previous_height = 0.0

        def flush() -> None:
            if paragraph:
                positioned.append((paragraph_top, Paragraph(text=" ".join(paragraph))))
                paragraph.clear()

        for line in lines:
            text = str(line.get("text", "")).strip()
            if not text:
                continue
            top, bottom = float(line["top"]), float(line["bottom"])
            size = max((float(c.get("size", 0)) for c in line.get("chars", [])), default=0.0)
            level = self._heading_level(size, body, text)
            if level:
                flush()
                positioned.append((top, Heading(level=level, text=text)))
                previous_bottom = None
                continue
            height = bottom - top
            gap = top - previous_bottom if previous_bottom is not None else 0.0
            if paragraph and gap > max(previous_height, height) * 0.9:
                flush()
            if not paragraph:
                paragraph_top = top
            paragraph.append(text)
            previous_bottom, previous_height = bottom, height
        flush()
        positioned.sort(key=lambda item: item[0])
        return [block for _, block in positioned], bool(lines) or bool(boxes)

    @staticmethod
    def _body_size(lines: list[dict[str, Any]]) -> float:
        sizes: Counter[float] = Counter()
        for line in lines:
            for char in line.get("chars", []):
                sizes[round(float(char.get("size", 0)), 1)] += 1
        return sizes.most_common(1)[0][0] if sizes else 0.0

    @staticmethod
    def _heading_level(size: float, body: float, text: str) -> int | None:
        if body <= 0 or len(text) > 120:
            return None
        ratio = size / body
        for threshold, level in _HEADING_RATIOS:
            if ratio >= threshold:
                return level
        return None

    @staticmethod
    def _ocr_pages(
        path: Path,
        pages: list[int],
        sections: list[Section],
        warnings: list[ExtractionWarning],
        ctx: ExtractContext,
    ) -> None:
        if ctx.ocr is OcrMode.OFF or not ocr.ocr_available():
            reason = "OCR is off" if ctx.ocr is OcrMode.OFF else "no OCR backend available"
            for number in pages:
                warnings.append(
                    ExtractionWarning(
                        code="SCANNED_PAGE",
                        message=f"page has no text layer; {reason}",
                        location=f"page {number}",
                    )
                )
            return
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(str(path))
        try:
            with tempfile.TemporaryDirectory(prefix="dossier-ocr-") as tmp:
                for number in pages:
                    image = Path(tmp) / f"page-{number}.png"
                    bitmap = document[number - 1].render(scale=_OCR_DPI / 72)
                    bitmap.to_pil().save(image)
                    paragraphs, page_warnings = ocr.recognize(image, ctx, location=f"page {number}")
                    warnings.extend(page_warnings)
                    if paragraphs:
                        sections[number - 1].blocks = [Paragraph(text=p) for p in paragraphs]
                    else:
                        warnings.append(
                            ExtractionWarning(
                                code="SCANNED_PAGE",
                                message="page has no text layer and OCR found no text",
                                location=f"page {number}",
                            )
                        )
        finally:
            document.close()


class ImageExtractor:
    name = "image"
    version = 1
    suffixes = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".heic", ".bmp", ".gif", ".webp")

    def extract(self, path: Path, ctx: ExtractContext) -> ExtractionResult:
        blocks: list[Block] = []
        warnings: list[ExtractionWarning] = []
        if ctx.ocr is OcrMode.OFF:
            warnings.append(ExtractionWarning(code="OCR_OFF", message="image text not read"))
        elif not ocr.ocr_available():
            warnings.append(
                ExtractionWarning(code="OCR_UNAVAILABLE", message="no OCR backend available")
            )
        else:
            paragraphs, warnings = ocr.recognize(path, ctx, location="image")
            blocks.extend(Paragraph(text=p) for p in paragraphs)
            if not paragraphs:
                warnings.append(ExtractionWarning(code="NO_TEXT", message="OCR found no text"))
        return ExtractionResult(
            document=DocumentIR(
                source_type="image",
                sections=[Section(kind=SectionKind.BODY, blocks=blocks)],
            ),
            warnings=warnings,
        )
