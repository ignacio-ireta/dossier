"""Word and PowerPoint extraction (python-docx, python-pptx)."""

from __future__ import annotations

import contextlib
import re
from pathlib import Path, PurePosixPath
from typing import Any

from dossier.convert.extractors.base import ExtractContext, ExtractionError
from dossier.convert.model import (
    Block,
    DocumentIR,
    ExtractionResult,
    ExtractionWarning,
    Heading,
    ImageRef,
    ListItem,
    Paragraph,
    Section,
    SectionKind,
    Table,
)

_HEADING_STYLE = re.compile(r"^Heading (\d)$")
_LIST_STYLE = re.compile(r"^List (Bullet|Number)(?: (\d))?$")


def _open_error(exc: Exception, path: Path) -> ExtractionError:
    text = str(exc).lower()
    if "encrypt" in text or "password" in text:
        return ExtractionError("file is password protected", code="password_protected", path=path)
    return ExtractionError(
        f"cannot open file ({type(exc).__name__})", code="corrupt_file", path=path
    )


class DocxExtractor:
    name = "docx"
    version = 1
    suffixes = (".docx", ".docm")

    def extract(self, path: Path, ctx: ExtractContext) -> ExtractionResult:
        del ctx
        import docx
        from docx.table import Table as DocxTable
        from docx.text.paragraph import Paragraph as DocxParagraph

        try:
            document = docx.Document(str(path))
        except Exception as exc:
            raise _open_error(exc, path) from exc
        blocks: list[Block] = []
        for item in document.iter_inner_content():
            if isinstance(item, DocxParagraph):
                blocks.extend(self._paragraph(item))
            elif isinstance(item, DocxTable):
                blocks.append(Table(rows=[[c.text for c in row.cells] for row in item.rows]))
        warnings = self._unsupported(document)
        core = document.core_properties
        return ExtractionResult(
            document=DocumentIR(
                source_type="docx",
                title=core.title or None,
                sections=[Section(kind=SectionKind.BODY, blocks=blocks)],
                properties=_core_dates(core),
            ),
            warnings=warnings,
        )

    def _paragraph(self, paragraph: Any) -> list[Block]:
        from docx.text.hyperlink import Hyperlink
        from docx.text.run import Run

        pieces: list[str] = []
        for item in paragraph.iter_inner_content():
            if isinstance(item, Run):
                pieces.append(item.text)
            elif isinstance(item, Hyperlink):
                pieces.append(f"[{item.text}]({item.address})" if item.address else item.text)
        text = "".join(pieces)
        style = ""
        with contextlib.suppress(AttributeError, KeyError):
            style = str(paragraph.style.name or "") if paragraph.style is not None else ""
        blocks: list[Block] = []
        heading = _HEADING_STYLE.match(style)
        listed = _LIST_STYLE.match(style)
        if style == "Title":
            blocks.append(Heading(level=1, text=text))
        elif heading:
            blocks.append(Heading(level=min(int(heading.group(1)), 6), text=text))
        elif listed:
            level = int(listed.group(2)) - 1 if listed.group(2) else self._ilvl(paragraph)
            blocks.append(ListItem(text=text, level=level, ordered=listed.group(1) == "Number"))
        elif self._is_numbered(paragraph) and text.strip():
            blocks.append(ListItem(text=text, level=self._ilvl(paragraph)))
        elif text.strip():
            blocks.append(Paragraph(text=text))
        blocks.extend(self._images(paragraph))
        return blocks

    @staticmethod
    def _is_numbered(paragraph: Any) -> bool:
        try:
            return paragraph._p.pPr.numPr is not None
        except AttributeError:
            return False

    @staticmethod
    def _ilvl(paragraph: Any) -> int:
        try:
            return int(paragraph._p.pPr.numPr.ilvl.val)
        except (AttributeError, TypeError, ValueError):
            return 0

    @staticmethod
    def _images(paragraph: Any) -> list[ImageRef]:
        images: list[ImageRef] = []
        try:
            rids = paragraph._p.xpath(".//a:blip/@r:embed")
        except Exception:
            return images
        for rid in rids:
            part = paragraph.part.related_parts.get(rid)
            name = PurePosixPath(str(part.partname)).name if part is not None else None
            images.append(ImageRef(name=name))
        return images

    @staticmethod
    def _unsupported(document: Any) -> list[ExtractionWarning]:
        body = document.element.body
        checks = (
            (".//w:footnoteReference", "FOOTNOTES_NOT_EXTRACTED", "footnotes"),
            (".//w:commentReference", "COMMENTS_NOT_EXTRACTED", "comments"),
            (".//w:ins | .//w:del", "TRACKED_CHANGES_IGNORED", "tracked changes"),
        )
        warnings = []
        for xpath, code, what in checks:
            with contextlib.suppress(Exception):
                if body.xpath(xpath):
                    warnings.append(
                        ExtractionWarning(code=code, message=f"document has {what}; not extracted")
                    )
        return warnings


class PptxExtractor:
    name = "pptx"
    version = 1
    suffixes = (".pptx", ".pptm")

    def extract(self, path: Path, ctx: ExtractContext) -> ExtractionResult:
        del ctx
        from pptx import Presentation

        try:
            presentation = Presentation(str(path))
        except Exception as exc:
            raise _open_error(exc, path) from exc
        sections: list[Section] = []
        for index, slide in enumerate(presentation.slides, start=1):
            title = None
            with contextlib.suppress(AttributeError, KeyError):
                if slide.shapes.title is not None and slide.shapes.title.text.strip():
                    title = slide.shapes.title.text
            blocks: list[Block] = []
            self._walk(slide.shapes, blocks)
            sections.append(
                Section(kind=SectionKind.SLIDE, index=index, title=title, blocks=blocks)
            )
            notes = self._notes(slide)
            if notes:
                sections.append(
                    Section(kind=SectionKind.NOTES, index=index, blocks=[Paragraph(text=notes)])
                )
        core = presentation.core_properties
        return ExtractionResult(
            document=DocumentIR(
                source_type="pptx",
                title=core.title or None,
                sections=sections,
                properties={**_core_dates(core), "Slides": str(len(presentation.slides))},
            )
        )

    def _walk(self, shapes: Any, blocks: list[Block]) -> None:
        from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

        title_types = {PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE}
        list_types = {PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT}
        for shape in shapes:
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                self._walk(shape.shapes, blocks)
                continue
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                alt = None
                with contextlib.suppress(AttributeError):
                    alt = shape._element._nvXxPr.cNvPr.get("descr") or None
                blocks.append(ImageRef(name=getattr(shape, "name", None), alt=alt))
                continue
            if getattr(shape, "has_table", False):
                rows = [[cell.text for cell in row.cells] for row in shape.table.rows]
                blocks.append(Table(rows=rows))
                continue
            if not getattr(shape, "has_text_frame", False):
                continue
            placeholder_type = None
            with contextlib.suppress(AttributeError, ValueError):
                if shape.is_placeholder:
                    placeholder_type = shape.placeholder_format.type
            if placeholder_type in title_types:
                continue
            as_list = placeholder_type in list_types
            for paragraph in shape.text_frame.paragraphs:
                text = "".join(run.text for run in paragraph.runs)
                if not text.strip():
                    continue
                if as_list:
                    blocks.append(ListItem(text=text, level=paragraph.level or 0))
                else:
                    blocks.append(Paragraph(text=text))

    @staticmethod
    def _notes(slide: Any) -> str | None:
        try:
            if not slide.has_notes_slide:
                return None
            text = str(slide.notes_slide.notes_text_frame.text)
        except (AttributeError, ValueError):
            return None
        return text if text.strip() else None


def _core_dates(core: Any) -> dict[str, str]:
    """Creation/modification dates only: author fields are personal data."""
    props: dict[str, str] = {}
    if getattr(core, "created", None):
        props["Created"] = core.created.date().isoformat()
    if getattr(core, "modified", None):
        props["Modified"] = core.modified.date().isoformat()
    return props
