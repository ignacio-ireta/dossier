"""``dsr docs render``: Markdown -> DOCX, PPTX or PDF through pandoc and a template pack.

DOCX: pandoc with the kind's reference document (styles, headers, footers), an
optional cover page in front, ``{{tokens}}`` filled from front matter and pack
defaults, and fields refreshed when Word opens the file.
PPTX: pandoc with the kind's PowerPoint template (layouts, theme); slides split
at level-2 headings (``slide-level`` in front matter changes that).
PDF: pandoc to Typst with the kind's theme, compiled by ``typst``; or
``--via docx`` to go through the Word template and LibreOffice for exact
corporate formatting.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dossier.core.errors import DossierError, ExternalToolError
from dossier.core.paths import user_cache_dir
from dossier.core.workspace import Workspace
from dossier.docs.diagrams import prerender
from dossier.docs.docx_tools import fill_tokens, insert_cover, update_fields_on_open
from dossier.docs.packs import ResolvedKind, resolve
from dossier.docs.toc import bake_toc_pages, prefill_toc
from dossier.ops.tools import tool

FORMATS = ("docx", "pptx", "pdf")
_IMAGE = re.compile(r"(!\[[^\]]*\]\()([^)\s]+)(\))")


@dataclass
class RenderResult:
    output: Path
    format: str
    pack: str
    kind: str
    diagrams: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "output": str(self.output),
            "format": self.format,
            "template": f"{self.pack}:{self.kind}",
            "diagrams": self.diagrams,
            "warnings": self.warnings,
        }


def front_matter(text: str) -> dict[str, str]:
    """Scalar fields of a YAML front-matter block (lists are joined with ', ')."""
    match = re.match(r"\A---\n(.*?)\n(---|\.\.\.)\n", text, re.DOTALL)
    if not match:
        return {}
    fields: dict[str, str] = {}
    current: str | None = None
    for line in match.group(1).splitlines():
        item = re.match(r"^\s+-\s+(.*)$", line)
        if item and current:
            value = item.group(1).strip().strip("'\"")
            fields[current] = f"{fields[current]}, {value}" if fields[current] else value
            continue
        pair = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if pair:
            current = pair.group(1)
            value = pair.group(2).strip()
            if value.startswith("[") and value.endswith("]"):
                value = ", ".join(v.strip().strip("'\"") for v in value[1:-1].split(","))
            fields[current] = value.strip("'\"")
    return fields


def _absolute_images(markdown: str, base_dir: Path) -> str:
    def fix(match: re.Match[str]) -> str:
        target = match.group(2)
        if re.match(r"^[a-z]+://", target) or Path(target).is_absolute():
            return match.group(0)
        return f"{match.group(1)}{(base_dir / target).resolve()}{match.group(3)}"

    return _IMAGE.sub(fix, markdown)


def _pandoc() -> str:
    found = tool("pandoc").locate()
    if found is None:
        raise ExternalToolError(f"rendering needs pandoc: {tool('pandoc').install_hint()}")
    return str(found)


def _run(command: list[str], what: str, cwd: Path | None = None) -> None:
    completed = subprocess.run(
        command, check=False, capture_output=True, text=True, timeout=900, cwd=cwd
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:500]
        raise ExternalToolError(f"{what} failed: {detail}")


def _tokens(resolved: ResolvedKind, front: dict[str, str]) -> dict[str, str]:
    values = dict(resolved.defaults)
    values.update({k: v for k, v in front.items() if v})
    values.setdefault("date", date.today().isoformat())
    values.setdefault("title", "")
    return values


def _docx(markdown: Path, out: Path, resolved: ResolvedKind, front: dict[str, str]) -> list[str]:
    if resolved.docx is None:
        raise DossierError(f"{resolved.pack}:{resolved.kind} has no DOCX reference")
    command = [
        _pandoc(),
        str(markdown),
        "-f",
        "markdown",
        "-t",
        "docx",
        "-o",
        str(out),
        "--reference-doc",
        str(resolved.docx),
    ]
    toc = resolved.toc or front.get("toc", "").lower() == "true"
    if toc:
        command += ["--toc", "--toc-depth=3"]
    if resolved.number_sections:
        command.append("--number-sections")
    _run(command, "pandoc (docx)")
    if resolved.cover is not None:
        insert_cover(out, resolved.cover)
    unknown = fill_tokens(out, _tokens(resolved, front))
    warnings = [f"no value for token {{{{{name}}}}} (left empty)" for name in unknown]
    if toc:
        soffice = tool("soffice").locate()
        if soffice is not None:
            result = bake_toc_pages(out, soffice)
            if result.missing:
                warnings.append(f"TOC: {len(result.missing)} heading page(s) not located")
        else:
            import docx

            document = docx.Document(str(out))
            prefill_toc(document, 3)
            document.save(str(out))
            update_fields_on_open(out)
            warnings.append(
                "TOC page numbers are filled when Word opens the file "
                "(install LibreOffice to bake them in)"
            )
    return warnings


def _pptx(markdown: Path, out: Path, resolved: ResolvedKind, front: dict[str, str]) -> None:
    if resolved.pptx is None:
        raise DossierError(f"{resolved.pack}:{resolved.kind} has no PowerPoint template")
    level = front.get("slide-level", "2")
    _run(
        [
            _pandoc(),
            str(markdown),
            "-f",
            "markdown",
            "-t",
            "pptx",
            "-o",
            str(out),
            "--reference-doc",
            str(resolved.pptx),
            f"--slide-level={level}",
        ],
        "pandoc (pptx)",
    )


def _pdf_typst(markdown: Path, out: Path, resolved: ResolvedKind, front: dict[str, str]) -> None:
    typst = tool("typst").locate()
    if typst is None:
        raise ExternalToolError(f"PDF needs typst: {tool('typst').install_hint()}")
    with tempfile.TemporaryDirectory(prefix="dossier-pdf-") as tmp:
        work = Path(tmp)
        command = [
            _pandoc(),
            str(markdown),
            "-f",
            "markdown",
            "-t",
            "typst",
            "-s",
            "-o",
            str(work / "document.typ"),
        ]
        if resolved.pdf_theme is not None:
            shutil.copy2(resolved.pdf_theme, work / "theme.typ")
            command += ["-V", "template=theme.typ"]
        for key, value in resolved.pdf_vars.items():
            command += ["-V", f"{key}={value}"]
        if resolved.toc or front.get("toc", "").lower() == "true":
            command.append("--toc")
        if resolved.number_sections:
            command.append("--number-sections")
        _run(command, "pandoc (typst)")
        _run([str(typst), "compile", "--root", "/", str(work / "document.typ"), str(out)], "typst")


def _pdf_via_docx(
    markdown: Path, out: Path, resolved: ResolvedKind, front: dict[str, str]
) -> list[str]:
    soffice = tool("soffice").locate()
    if soffice is None:
        raise ExternalToolError(f"--via docx needs LibreOffice: {tool('soffice').install_hint()}")
    with tempfile.TemporaryDirectory(prefix="dossier-pdf-") as tmp:
        work = Path(tmp)
        docx_out = work / f"{out.stem}.docx"
        warnings = _docx(markdown, docx_out, resolved, front)
        _run(
            [
                str(soffice),
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(work),
                str(docx_out),
            ],
            "LibreOffice",
        )
        shutil.move(str(work / f"{out.stem}.pdf"), out)
    return warnings


def render_document(
    workspace: Workspace | None,
    source: Path,
    to: str,
    *,
    template: str | None = None,
    out: Path | None = None,
    via_docx: bool = False,
) -> RenderResult:
    if to not in FORMATS:
        raise DossierError(f"unknown format {to!r}; choose from {', '.join(FORMATS)}")
    if not source.is_file():
        raise DossierError(f"{source} not found")
    text = source.read_text(encoding="utf-8")
    front = front_matter(text)
    resolved = resolve(workspace, template=template, front=front, output=to)
    if out is None:
        folder = workspace.paths.out if workspace else source.parent
        out = folder / f"{source.stem}.{to}"
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    cache = (workspace.paths.cache if workspace else user_cache_dir()) / "diagrams"
    body, diagrams = prerender(
        _absolute_images(text, source.parent.resolve()),
        base_dir=source.parent.resolve(),
        cache_dir=cache,
        fmt="svg" if to == "pdf" and not via_docx else "png",
    )
    result = RenderResult(out, to, resolved.pack, resolved.kind, diagrams=len(diagrams))
    with tempfile.TemporaryDirectory(prefix="dossier-render-") as tmp:
        prepared = Path(tmp) / "document.md"
        prepared.write_text(body, encoding="utf-8")
        if to == "docx":
            result.warnings += _docx(prepared, out, resolved, front)
        elif to == "pptx":
            _pptx(prepared, out, resolved, front)
        elif via_docx:
            result.warnings += _pdf_via_docx(prepared, out, resolved, front)
        else:
            _pdf_typst(prepared, out, resolved, front)
    return result
