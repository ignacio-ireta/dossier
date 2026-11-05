"""DOCX post-processing: tokens, cover pages, field updates.

Tokens are ``{{name}}`` placeholders in a template's body, headers, footers or
text boxes (``{{title}}``, ``{{subtitle}}``, ``{{author}}``, ``{{date}}``,
``{{version}}``, ``{{doc_code}}``, or any key from the pack defaults or the
document's front matter). Word often splits typed text across several runs, so
``tokenize`` rewrites a whole paragraph when it finds a literal to replace.
"""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from xml.sax.saxutils import escape

from dossier.docs.packs import TOKEN

_PARTS = re.compile(r"word/(document|header\d*|footer\d*)\.xml")


def _rewrite(path: Path, edit: Callable[[str, bytes], bytes]) -> None:
    mode = path.stat().st_mode & 0o777
    fd, temporary = tempfile.mkstemp(prefix=".dossier-docx-", suffix=".docx", dir=path.parent)
    os.close(fd)
    try:
        with (
            zipfile.ZipFile(path) as source,
            zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as target,
        ):
            for item in source.infolist():
                target.writestr(item, edit(item.filename, source.read(item.filename)))
        Path(temporary).chmod(mode)  # mkstemp creates 0600; keep the original permissions
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def fill_tokens(path: Path, values: dict[str, str]) -> list[str]:
    """Replace {{tokens}}; unknown tokens become empty. Returns the unknown names."""
    unknown: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return escape(values[key])
        unknown.add(key)
        return ""

    def edit(name: str, data: bytes) -> bytes:
        if not _PARTS.fullmatch(name):
            return data
        return TOKEN.sub(replace, data.decode("utf-8")).encode("utf-8")

    _rewrite(path, edit)
    return sorted(unknown)


def update_fields_on_open(path: Path) -> None:
    """Ask Word to refresh fields (table of contents, page numbers) when opened."""

    def edit(name: str, data: bytes) -> bytes:
        if name != "word/settings.xml":
            return data
        xml = data.decode("utf-8")
        if "w:updateFields" in xml:
            return data
        xml = re.sub(r"(<w:settings[^>]*>)", r'\1<w:updateFields w:val="true"/>', xml, count=1)
        return xml.encode("utf-8")

    _rewrite(path, edit)


def tokenize(path: Path, replacements: dict[str, str]) -> int:
    """Replace literal text with tokens everywhere (also across split runs)."""
    import docx
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph

    document = docx.Document(str(path))
    parts: list[Any] = [document.part]
    for section in document.sections:
        for hf in (
            section.header,
            section.first_page_header,
            section.even_page_header,
            section.footer,
            section.first_page_footer,
            section.even_page_footer,
        ):
            if not hf.is_linked_to_previous:
                parts.append(hf.part)
    changed = 0
    seen: set[int] = set()
    for part in parts:
        if id(part) in seen:
            continue
        seen.add(id(part))
        for element in part.element.iter(qn("w:p")):
            paragraph = Paragraph(element, cast(Any, None))
            runs = paragraph.runs
            if not runs:
                continue
            full = "".join(run.text for run in runs)
            updated = full
            for literal, token in replacements.items():
                updated = updated.replace(literal, token)
            if updated != full:
                runs[0].text = updated
                for run in runs[1:]:
                    run.text = ""
                changed += 1
    document.save(str(path))
    return changed


def insert_cover(body: Path, cover: Path) -> None:
    """Put the cover's content in front of the body, followed by a page break.

    The body keeps its own section settings (header, footer, margins).
    """
    import docx
    from docx.enum.text import WD_BREAK
    from docxcompose.composer import Composer

    cover_doc = docx.Document(str(cover))
    cover_doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    master = docx.Document(str(body))
    composer = Composer(master)
    composer.insert(0, cover_doc)
    composer.save(str(body))
