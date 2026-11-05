"""OCR through the platform seam, plus grouping recognized lines into paragraphs."""

from __future__ import annotations

import statistics
from pathlib import Path

from dossier import platform
from dossier.convert.extractors.base import ExtractContext
from dossier.convert.model import ExtractionWarning
from dossier.platform.base import OcrLine

LOW_CONFIDENCE = 0.5


def ocr_available() -> bool:
    return platform.current().ocr.available()


def group_into_paragraphs(lines: list[OcrLine], gap_factor: float = 1.0) -> list[str]:
    """Join lines into paragraphs, splitting where the vertical gap is large."""
    boxed = [line for line in lines if line.text.strip() and line.box]
    if not boxed:
        return [line.text for line in lines if line.text.strip()]
    boxed.sort(key=lambda line: (round(line.box[1], 3), line.box[0]))  # type: ignore[index]
    heights = [line.box[3] for line in boxed if line.box]
    threshold = statistics.median(heights) * gap_factor
    paragraphs: list[list[str]] = [[boxed[0].text]]
    previous = boxed[0]
    for line in boxed[1:]:
        assert line.box and previous.box
        gap = line.box[1] - (previous.box[1] + previous.box[3])
        if threshold > 0 and gap > threshold:
            paragraphs.append([line.text])
        else:
            paragraphs[-1].append(line.text)
        previous = line
    return [" ".join(parts) for parts in paragraphs]


def recognize(
    image: Path, ctx: ExtractContext, *, location: str
) -> tuple[list[str], list[ExtractionWarning]]:
    """OCR one image file. Returns paragraphs and warnings (never raises on no text)."""
    lines = platform.current().ocr.recognize(image, languages=ctx.ocr_languages)
    paragraphs = group_into_paragraphs(lines)
    warnings: list[ExtractionWarning] = []
    if paragraphs:
        warnings.append(
            ExtractionWarning(
                code="OCR_APPLIED",
                message=f"{len(lines)} line(s) recognized by OCR",
                location=location,
            )
        )
        confidences = [line.confidence for line in lines if line.text.strip()]
        mean = sum(confidences) / len(confidences) if confidences else 0.0
        if mean < LOW_CONFIDENCE:
            warnings.append(
                ExtractionWarning(
                    code="OCR_LOW_CONFIDENCE",
                    message=f"mean OCR confidence is low ({mean:.2f})",
                    location=location,
                )
            )
    return paragraphs, warnings
