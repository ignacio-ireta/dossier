"""A table of contents that is filled in before anyone opens the document.

pandoc writes an empty TOC field and relies on Word refreshing it on open (with
an "update fields?" prompt); LibreOffice and PDF exports show it blank. Here:

- :func:`prefill_toc` writes real entries (``toc N`` style, dot-leader tab, link
  to the heading's bookmark) inside the original TOC field, so Word can still
  refresh it with F9 and the TOC looks right everywhere;
- :func:`bake_toc_pages` renders the document with headless LibreOffice, finds
  each heading's page, writes the numbers in, and repeats until pagination is
  stable; the update prompt is then removed.

Page numbers are physical pages, equal to the footer's PAGE field unless the
document restarts numbering.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_LEADER = re.compile(r"[.…·]{3,}\s*\d+")


def qn(tag: str) -> str:
    prefix, _, local = tag.partition(":")
    return f"{{{W}}}{local}" if prefix == "w" else tag


@dataclass
class Heading:
    level: int
    text: str
    anchor: str | None


@dataclass
class TocResult:
    entries: int = 0
    passes: int = 0
    stable: bool = False
    missing: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()


def _key(text: str) -> str:
    return _norm(text).casefold()


def _style_ids(document: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for style in document.styles.element.findall(qn("w:style")):
        name = style.find(qn("w:name"))
        if name is not None:
            out[(name.get(qn("w:val")) or "").lower()] = style.get(qn("w:styleId"))
    return out


def _toc_sdt(body: Any) -> Any:
    for sdt in body.iter(qn("w:sdt")):
        gallery = sdt.find(f".//{qn('w:docPartGallery')}")
        if gallery is not None and gallery.get(qn("w:val")) == "Table of Contents":
            return sdt
    return None


def _content_width(body: Any) -> int:
    sect = body.find(qn("w:sectPr"))
    try:
        size = sect.find(qn("w:pgSz"))
        margins = sect.find(qn("w:pgMar"))
        return (
            int(size.get(qn("w:w")))
            - int(margins.get(qn("w:left")))
            - int(margins.get(qn("w:right")))
        )
    except (AttributeError, TypeError, ValueError):
        return 9360


def collect_headings(body: Any, heading_ids: dict[str, int], depth: int) -> list[Heading]:
    sdt = _toc_sdt(body)
    after = sdt is None
    heads: list[Heading] = []
    pending: str | None = None
    for element in body:
        if element is sdt:
            after = True
            continue
        if not after:
            continue
        if element.tag == qn("w:bookmarkStart"):
            pending = element.get(qn("w:name"))
            continue
        if element.tag != qn("w:p"):
            if element.tag != qn("w:bookmarkEnd"):
                pending = None
            continue
        style = element.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
        level = heading_ids.get(style.get(qn("w:val"))) if style is not None else None
        if level is not None and level <= depth:
            text = _norm("".join(t.text or "" for t in element.iter(qn("w:t"))))
            inner = element.find(qn("w:bookmarkStart"))
            anchor = pending or (inner.get(qn("w:name")) if inner is not None else None)
            if text:
                heads.append(Heading(level, text, anchor))
        pending = None
    return heads


def _run(children: str) -> str:
    return f"<w:r>{children}</w:r>"


def _entry(h: Heading, page: str, style_id: str | None, tab: int, begin: str, end: bool) -> str:
    text = h.text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    style = f'<w:pStyle w:val="{style_id}"/>' if style_id else ""
    inner = (
        _run(f'<w:rPr><w:noProof/></w:rPr><w:t xml:space="preserve">{text}</w:t>')
        + _run("<w:rPr><w:noProof/><w:webHidden/></w:rPr><w:tab/>")
        + _run(f"<w:rPr><w:noProof/><w:webHidden/></w:rPr><w:t>{page}</w:t>")
    )
    runs = begin + (
        f'<w:hyperlink w:anchor="{h.anchor}" w:history="1">{inner}</w:hyperlink>'
        if h.anchor
        else inner
    )
    if end:
        runs += _run('<w:fldChar w:fldCharType="end"/>')
    return (
        f'<w:p xmlns:w="{W}"><w:pPr>{style}'
        f'<w:tabs><w:tab w:val="right" w:leader="dot" w:pos="{tab}"/></w:tabs>'
        f"<w:rPr><w:noProof/></w:rPr></w:pPr>{runs}</w:p>"
    )


def prefill_toc(document: Any, depth: int, pages: dict[int, str] | None = None) -> list[Heading]:
    from docx.oxml import parse_xml

    body = document.element.body
    sdt = _toc_sdt(body)
    if sdt is None:
        return []
    names = _style_ids(document)
    heading_ids = {names[f"heading {n}"]: n for n in range(1, 10) if f"heading {n}" in names}
    heads = collect_headings(body, heading_ids, depth)
    if not heads:
        return []
    content = sdt.find(qn("w:sdtContent"))
    paragraphs = content.findall(qn("w:p"))
    instruction = next(
        (
            (i.text or "").strip()
            for i in content.iter(qn("w:instrText"))
            if "TOC" in (i.text or "")
        ),
        f'TOC \\o "1-{depth}" \\h \\z \\u',
    )
    keep = paragraphs[0] if paragraphs and not list(paragraphs[0].iter(qn("w:fldChar"))) else None
    for paragraph in paragraphs:
        if paragraph is not keep:
            content.remove(paragraph)
    tab = _content_width(body)
    begin = (
        _run('<w:fldChar w:fldCharType="begin"/>')
        + _run(f'<w:instrText xml:space="preserve"> {instruction} </w:instrText>')
        + _run('<w:fldChar w:fldCharType="separate"/>')
    )
    for index, heading in enumerate(heads):
        xml = _entry(
            heading,
            (pages or {}).get(index, ""),
            names.get(f"toc {heading.level}"),
            tab,
            begin if index == 0 else "",
            index == len(heads) - 1,
        )
        content.append(parse_xml(xml))
    return heads


def clear_update_prompt(document: Any) -> None:
    settings = document.settings.element
    for element in settings.findall(qn("w:updateFields")):
        settings.remove(element)
    for fld in document.element.body.iter(qn("w:fldChar")):
        if fld.get(qn("w:dirty")) is not None:
            del fld.attrib[qn("w:dirty")]


def _page_texts(pdf: Path) -> list[str]:
    import pdfplumber

    with pdfplumber.open(str(pdf)) as handle:
        return [_norm(page.extract_text() or "") for page in handle.pages]


def locate(texts: list[str], heads: list[Heading]) -> tuple[dict[int, str], list[str]]:
    if not heads:
        return {}, []
    keyed = [_key(t) for t in texts]
    last = _key(heads[-1].text)
    toc_end = next((i for i, t in enumerate(keyed) if last in t), -1)
    if toc_end >= 0:
        leaders = list(_LEADER.finditer(keyed[toc_end]))
        if leaders:
            keyed[toc_end] = keyed[toc_end][leaders[-1].end() :]
    pages: dict[int, str] = {}
    missing: list[str] = []
    start = max(toc_end, 0)
    for index, heading in enumerate(heads):
        target = _key(heading.text)
        found = next((j for j in range(start, len(keyed)) if target in keyed[j]), None)
        if found is None:
            missing.append(heading.text)
            continue
        pages[index] = str(found + 1)
        start = found
    return pages, missing


def soffice_pdf(docx_path: Path, soffice: Path, out_dir: Path) -> Path:
    profile = out_dir / "lo-profile"
    subprocess.run(
        [
            str(soffice),
            f"-env:UserInstallation={profile.resolve().as_uri()}",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(out_dir),
            str(docx_path),
        ],
        check=True,
        capture_output=True,
        timeout=600,
    )
    return out_dir / f"{docx_path.stem}.pdf"


def bake_toc_pages(path: Path, soffice: Path, depth: int = 3, max_passes: int = 3) -> TocResult:
    import docx

    result = TocResult()
    previous: dict[int, str] | None = None
    with tempfile.TemporaryDirectory(prefix="dossier-toc-") as tmp:
        work = Path(tmp)
        for number in range(1, max_passes + 1):
            document = docx.Document(str(path))
            heads = prefill_toc(document, depth, previous)
            if not heads:
                return result
            clear_update_prompt(document)
            staged = work / f"pass{number}" / path.name
            staged.parent.mkdir(parents=True)
            document.save(str(staged))
            pages, missing = locate(_page_texts(soffice_pdf(staged, soffice, staged.parent)), heads)
            result.passes, result.entries, result.missing = number, len(heads), missing
            if pages == previous:
                result.stable = True
                break
            previous = pages
        document = docx.Document(str(path))
        prefill_toc(document, depth, previous)
        clear_update_prompt(document)
        document.save(str(path))
    return result
