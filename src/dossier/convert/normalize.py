"""Text hygiene between extraction and rendering.

Unicode NFC, control-character stripping, whitespace collapsing, line-ending
normalization, and dropping blocks that end up empty. Markdown escaping belongs
to the writer, not here.
"""

from __future__ import annotations

import re
import unicodedata

from dossier.convert.model import (
    Block,
    CodeBlock,
    DocumentIR,
    Heading,
    ImageRef,
    ListItem,
    Paragraph,
    RawMarkdown,
    Section,
    Table,
)

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RUN = re.compile(r"[ \t\u00a0]+")


def clean_text(text: str, *, fold_newlines: bool = True) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL.sub("", text)
    if fold_newlines:
        return _WS_RUN.sub(" ", text.replace("\n", " ")).strip()
    return "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")


def _normalize_block(block: Block) -> Block | None:
    if isinstance(block, Heading):
        text = clean_text(block.text)
        return Heading(level=block.level, text=text) if text else None
    if isinstance(block, Paragraph):
        text = clean_text(block.text)
        return Paragraph(text=text) if text else None
    if isinstance(block, ListItem):
        text = clean_text(block.text)
        return ListItem(text=text, level=block.level, ordered=block.ordered) if text else None
    if isinstance(block, Table):
        rows = [[clean_text(cell) for cell in row] for row in block.rows]
        if not any(cell for row in rows for cell in row):
            return None
        caption = clean_text(block.caption) if block.caption else None
        return Table(
            rows=rows,
            has_header=block.has_header,
            caption=caption or None,
            total_rows=block.total_rows,
        )
    if isinstance(block, CodeBlock):
        text = clean_text(block.text, fold_newlines=False)
        return CodeBlock(text=text, language=block.language) if text else None
    if isinstance(block, RawMarkdown):
        text = clean_text(block.text, fold_newlines=False)
        return RawMarkdown(text=text) if text else None
    return ImageRef(
        name=clean_text(block.name) or None if block.name else None,
        alt=clean_text(block.alt) or None if block.alt else None,
    )


def normalize(doc: DocumentIR) -> DocumentIR:
    sections = [
        Section(
            kind=section.kind,
            index=section.index,
            title=clean_text(section.title) or None if section.title else None,
            blocks=[b for b in (_normalize_block(block) for block in section.blocks) if b],
        )
        for section in doc.sections
    ]
    return DocumentIR(
        source_type=doc.source_type,
        title=clean_text(doc.title) or None if doc.title else None,
        sections=sections,
        properties={clean_text(k): clean_text(v) for k, v in doc.properties.items()},
    )
