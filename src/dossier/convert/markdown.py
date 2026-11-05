"""Render a normalized :class:`DocumentIR` to Markdown.

Output contract (stable, so agents and the indexer can rely on it)::

    ---                      front matter: source, hash, type, title, extractor...
    ---
    # <title or file name>
    ## Slide 3: <title>      one H2 per section (slide, sheet, page, message);
    ...                      a plain body has no section heading and its own
                             headings start at H2
    ## Extraction warnings   only when there are warnings

Escaping happens here: pipes and newlines in table cells, and characters that
would turn plain text into Markdown structure at the start of a line.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from dossier.convert.model import (
    Block,
    CodeBlock,
    DocumentIR,
    ExtractionWarning,
    Heading,
    ImageRef,
    ListItem,
    Paragraph,
    RawMarkdown,
    Section,
    SectionKind,
    Table,
)

_ORDERED_MARKER = re.compile(r"^(\d+)([.)])(\s|$)")
_STRUCTURAL_MARKER = re.compile(r"^([#>*+-]|={3,}|-{3,})(\s|$)")

# Bump when the rendered Markdown changes, so existing outputs are regenerated.
WRITER_VERSION = 2
FRONT_MATTER_PROPERTIES = frozenset({"Created", "Modified", "Pages", "Slides", "Sheets"})

_SECTION_LABEL = {
    SectionKind.SLIDE: "Slide",
    SectionKind.NOTES: "Speaker notes, slide",
    SectionKind.SHEET: "Sheet",
    SectionKind.PAGE: "Page",
    SectionKind.MESSAGE: "Message",
    SectionKind.IMAGE: "Image",
}


def escape_leading_marker(text: str) -> str:
    match = _ORDERED_MARKER.match(text)
    if match:
        return f"{match.group(1)}\\{match.group(2)}{text[match.end(2) :]}"
    if _STRUCTURAL_MARKER.match(text):
        return f"\\{text}"
    return text


def _escape_cell(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def render_table(table: Table, *, full_data: str | None = None) -> str:
    rows = table.rows
    width = max((len(row) for row in rows), default=0)
    if width == 0:
        return ""
    padded = [row + [""] * (width - len(row)) for row in rows]
    if table.has_header:
        header, body = padded[0], padded[1:]
    else:
        header, body = [f"Column {i + 1}" for i in range(width)], padded
    lines = [
        "| " + " | ".join(_escape_cell(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(_escape_cell(cell) for cell in row) + " |" for row in body)
    parts = []
    if table.caption:
        parts.append(f"*{table.caption}*")
    parts.append("\n".join(lines))
    notes = []
    if table.total_rows is not None and table.total_rows > len(body):
        notes.append(f"Showing {len(body)} of {table.total_rows} data rows.")
    if full_data:
        notes.append(f"Full data: `{full_data}`.")
    if notes:
        parts.append(" ".join(notes))
    return "\n\n".join(parts)


def _section_heading(section: Section) -> str | None:
    if section.kind is SectionKind.BODY:
        return section.title
    label = _SECTION_LABEL.get(section.kind, section.kind.value.title())
    if section.kind is SectionKind.SHEET:
        return f"{label}: {section.title}" if section.title else label
    number = f" {section.index}" if section.index is not None else ""
    title = f": {section.title}" if section.title else ""
    return f"{label}{number}{title}"


def _render_block(block: Block, offset: int, sheet_csv: str | None) -> str:
    if isinstance(block, Heading):
        level = min(block.level + offset, 6)
        return f"{'#' * level} {block.text}"
    if isinstance(block, Paragraph):
        return escape_leading_marker(block.text)
    if isinstance(block, ListItem):
        marker = "1." if block.ordered else "-"
        return f"{'  ' * block.level}{marker} {escape_leading_marker(block.text)}"
    if isinstance(block, Table):
        return render_table(block, full_data=sheet_csv)
    if isinstance(block, CodeBlock):
        fence = "````" if "```" in block.text else "```"
        return f"{fence}{block.language or ''}\n{block.text}\n{fence}"
    if isinstance(block, RawMarkdown):
        return block.text
    assert isinstance(block, ImageRef)
    label = block.alt or block.name or "image"
    return f"*[Image: {label}]*"


def _join_blocks(blocks: Sequence[Block], offset: int, sheet_csv: str | None) -> str:
    out: list[str] = []
    previous_list = False
    for block in blocks:
        rendered = _render_block(block, offset, sheet_csv)
        if not rendered:
            continue
        is_list = isinstance(block, ListItem)
        if out and is_list and previous_list:
            out[-1] += "\n" + rendered
        else:
            out.append(rendered)
        previous_list = is_list
    return "\n\n".join(out)


def front_matter(fields: dict[str, str | int | None]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            continue
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def render(
    doc: DocumentIR,
    *,
    file_name: str,
    meta: dict[str, str | int | None],
    warnings: Sequence[ExtractionWarning] = (),
    sheet_csvs: dict[str, str] | None = None,
) -> str:
    # Dates and counts describe the file, not its content: keep them in front matter
    # so summaries and search see only what the document says.
    fields = dict(meta)
    body_props: dict[str, str] = {}
    for key, value in doc.properties.items():
        if key in FRONT_MATTER_PROPERTIES:
            fields[key.lower()] = value
        else:
            body_props[key] = value
    if doc.title and doc.title != file_name:
        fields["file"] = file_name
    parts = [front_matter(fields), f"# {doc.title or file_name}"]
    if body_props:
        parts.append("\n".join(f"- {key}: {value}" for key, value in body_props.items()))
    for section in doc.sections:
        heading = _section_heading(section)
        csv_path = (sheet_csvs or {}).get(section.title or "")
        if heading:
            parts.append(f"## {heading}")
            body = _join_blocks(section.blocks, 2, csv_path)
        else:
            body = _join_blocks(section.blocks, 1, csv_path)
        if body:
            parts.append(body)
    if warnings:
        parts.append("## Extraction warnings")
        parts.append(
            "\n".join(
                f"- `{w.code}`" + (f" ({w.location})" if w.location else "") + f": {w.message}"
                for w in warnings
            )
        )
    return "\n\n".join(parts).rstrip() + "\n"
