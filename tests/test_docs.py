from __future__ import annotations

import asyncio
import io
import shutil
import zipfile
from pathlib import Path

import docx
import pytest
from docx.shared import RGBColor

from dossier.core.errors import ConfigError, DossierError
from dossier.core.workspace import Workspace, open_workspace
from dossier.docs import diagrams
from dossier.docs.packs import (
    docx_tokens,
    import_file,
    inspect_pack,
    list_packs,
    load_pack,
    new_pack,
    resolve,
)
from dossier.docs.render import front_matter, render_document

needs_pandoc = pytest.mark.skipif(shutil.which("pandoc") is None, reason="needs pandoc")
NEUTRAL_REF = load_pack("neutral", None).resolve_kind("report").docx

DOC = """---
title: Falcon Assessment
subtitle: Current state
author:
  - Platform Team
version: "0.9"
---

# Summary

The platform consolidates reporting.

## Details

| A | B |
|---|---|
| 1 | 2 |
"""


def _reference(path: Path, color: RGBColor, header: str = "{{title}} | {{client}}") -> None:
    assert NEUTRAL_REF is not None
    document = docx.Document(str(NEUTRAL_REF))
    document.styles["Heading 1"].font.color.rgb = color
    document.sections[0].header.paragraphs[0].text = header
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))


def _cover(path: Path) -> None:
    from PIL import Image

    document = docx.Document()
    document.add_paragraph("{{client}}")
    document.add_paragraph("{{title}}")
    document.add_paragraph("Version {{version}}")
    logo = io.BytesIO()
    Image.new("RGB", (40, 20), "navy").save(logo, "PNG")
    logo.seek(0)
    document.add_picture(logo)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))


@pytest.fixture
def packs(workspace: Workspace) -> Workspace:
    root = workspace.paths.templates
    _reference(root / "clientA" / "report" / "reference.docx", RGBColor(0xC0, 0x00, 0x00))
    _cover(root / "clientA" / "report" / "cover.docx")
    (root / "clientA" / "pack.toml").write_text(
        'name = "clientA"\nextends = "neutral"\n[defaults]\nclient = "Client A"\n'
        '[kinds.report]\ndocx = "report/reference.docx"\ncover = "report/cover.docx"\n'
        "toc = false\n"
    )
    _reference(
        root / "clientB" / "proposal" / "reference.docx",
        RGBColor(0x00, 0x80, 0x00),
        header="{{client}} proposal",
    )
    (root / "clientB" / "pack.toml").write_text(
        'name = "clientB"\ndefault_kind = "proposal"\n[defaults]\nclient = "Client B"\n'
        '[kinds.proposal]\ndocx = "proposal/reference.docx"\n'
    )
    return open_workspace(workspace.root)


def _heading_color(path: Path) -> str:
    return str(docx.Document(str(path)).styles["Heading 1"].font.color.rgb)


def _all_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        return " ".join(
            archive.read(n).decode("utf-8")
            for n in archive.namelist()
            if n.startswith("word/") and n.endswith(".xml")
        )


def test_front_matter_parsing() -> None:
    fields = front_matter(DOC)
    assert fields["title"] == "Falcon Assessment" and fields["author"] == "Platform Team"
    assert fields["version"] == "0.9"
    assert front_matter("no front matter") == {}


def test_packs_listing_and_inheritance(packs: Workspace) -> None:
    names = {pack.name: pack.origin for pack in list_packs(packs)}
    assert names == {"clientA": "workspace", "clientB": "workspace", "neutral": "builtin"}
    client_a = load_pack("clientA", packs)
    assert [p.name for p in client_a.chain()] == ["clientA", "neutral"]
    assert set(client_a.kind_names()) == {"report", "memo", "deck"}
    report = client_a.resolve_kind("report")
    assert report.cover is not None and report.pptx is not None  # pptx inherited
    assert report.toc is False and report.defaults["client"] == "Client A"
    with pytest.raises(DossierError, match="no kind"):
        client_a.resolve_kind("brochure")


def test_resolution_order(packs: Workspace) -> None:
    assert resolve(packs, template=None, front={}, output="docx").pack == "neutral"
    front = {"template": "clientB"}
    assert resolve(packs, template=None, front=front, output="docx").kind == "proposal"
    picked = resolve(packs, template="clientA:report", front=front, output="docx")
    assert (picked.pack, picked.kind) == ("clientA", "report")
    deck = resolve(packs, template="clientA", front={"kind": "memo"}, output="pptx")
    assert deck.kind == "memo" or deck.pptx is not None
    config = packs.paths.config
    config.write_text(config.read_text().replace('template = "neutral"', 'template = "clientB"'))
    assert resolve(open_workspace(packs.root), template=None, front={}, output="docx").pack == (
        "clientB"
    )


def test_extends_loop_is_detected(workspace: Workspace) -> None:
    for name, parent in (("a", "b"), ("b", "a")):
        folder = workspace.paths.templates / name
        folder.mkdir(parents=True)
        (folder / "pack.toml").write_text(f'name = "{name}"\nextends = "{parent}"\n')
    with pytest.raises(ConfigError, match="loop"):
        load_pack("a", workspace)


@needs_pandoc
def test_two_client_packs_render_differently(packs: Workspace) -> None:
    source = packs.paths.out / "assessment.md"
    source.write_text(DOC)
    a = render_document(
        packs, source, "docx", template="clientA:report", out=packs.paths.out / "a.docx"
    )
    b = render_document(packs, source, "docx", template="clientB", out=packs.paths.out / "b.docx")
    neutral = render_document(packs, source, "docx", out=packs.paths.out / "n.docx")
    assert (a.pack, a.kind, b.pack, b.kind) == ("clientA", "report", "clientB", "proposal")
    assert _heading_color(a.output) == "C00000"
    assert _heading_color(b.output) == "008000"
    assert _heading_color(neutral.output) == "1F3864"
    text_a = _all_text(a.output)
    assert "Client A" in text_a and "Falcon Assessment | Client A" in text_a
    assert "Version 0.9" in text_a and "{{" not in text_a
    with zipfile.ZipFile(a.output) as archive:
        assert any(n.startswith("word/media/") for n in archive.namelist())  # cover logo kept
    assert "Client B proposal" in _all_text(b.output)


@needs_pandoc
def test_unknown_tokens_are_reported(workspace: Workspace) -> None:
    source = workspace.paths.out / "memo.md"
    source.write_text("---\ntitle: Memo\n---\n\nHello.\n")
    root = workspace.paths.templates / "branded"
    _reference(root / "memo" / "reference.docx", RGBColor(0, 0, 0), header="{{doc_code}}")
    (root / "pack.toml").write_text(
        'name = "branded"\n[kinds.memo]\ndocx = "memo/reference.docx"\n'
    )
    result = render_document(
        open_workspace(workspace.root), source, "docx", template="branded:memo"
    )
    assert result.warnings == ["no value for token {{doc_code}} (left empty)"]


def test_inspect_and_import(workspace: Workspace, tmp_path: Path) -> None:
    findings = inspect_pack(load_pack("neutral", workspace))
    assert not [f for f in findings if f.level == "error"]
    assert any("tokens: title" in f.message for f in findings)

    branded = tmp_path / "branded.docx"
    document = docx.Document()
    paragraph = document.add_paragraph()
    for piece in ("Quarterly ", "Status ", "Report"):
        paragraph.add_run(piece)  # Word-style split runs
    document.sections[0].header.paragraphs[0].add_run("V")
    document.sections[0].header.paragraphs[0].add_run("1.0")
    document.save(str(branded))
    target = import_file(
        workspace,
        branded,
        pack="acme",
        kind="report",
        role="cover",
        replacements={"Quarterly Status Report": "{{title}}", "V1.0": "{{version}}"},
    )
    tokens, split = docx_tokens(target)
    assert tokens == {"title", "version"} and split == 0
    pack = load_pack("acme", workspace)
    assert pack.spec.extends == "neutral" and pack.resolve_kind("report").cover == target
    errors = [f for f in inspect_pack(pack) if f.level == "error"]
    assert not errors
    (workspace.paths.templates / "acme" / "pack.toml").write_text(
        'name = "acme"\n[kinds.report]\ndocx = "missing.docx"\n'
    )
    assert any(f.level == "error" for f in inspect_pack(load_pack("acme", workspace)))
    with pytest.raises(DossierError, match="already exists"):
        new_pack(workspace, "acme")


def test_diagram_prerender_with_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rendered: list[str] = []

    def fake_mermaid(source: str, target: Path) -> None:
        rendered.append(source)
        target.write_bytes(b"png")

    def fake_drawio(source: Path, target: Path) -> None:
        rendered.append(source.name)
        target.write_bytes(b"png")

    monkeypatch.setattr(diagrams, "render_mermaid", fake_mermaid)
    monkeypatch.setattr(diagrams, "render_drawio", fake_drawio)
    (tmp_path / "flow.drawio").write_text("<mxfile/>")
    text = (
        "Intro\n\n```mermaid\n%% caption: Data flow\nflowchart LR\n  A --> B\n```\n\n"
        "![Flow](flow.drawio)\n"
    )
    out, made = diagrams.prerender(text, base_dir=tmp_path, cache_dir=tmp_path / "c", fmt="png")
    assert "```mermaid" not in out and "![Data flow](" in out and ".drawio)" not in out
    assert [d.kind for d in made] == ["mermaid", "drawio"]
    again, cached = diagrams.prerender(text, base_dir=tmp_path, cache_dir=tmp_path / "c", fmt="png")
    assert again == out and all(d.cached for d in cached) and len(rendered) == 2


@pytest.mark.skipif(
    shutil.which("mmdc") is None or diagrams.browser() is None, reason="needs mmdc and a browser"
)
def test_mermaid_real(tmp_path: Path) -> None:
    target = tmp_path / "d.svg"
    diagrams.render_mermaid("flowchart LR\n  A[Alpha] --> B[Beta]", target)
    svg = target.read_text()
    assert "Alpha" in svg and "foreignObject" not in svg  # plain SVG text labels


@pytest.mark.skipif(
    __import__("dossier.ops.tools", fromlist=["tool"]).tool("drawio").locate() is None,
    reason="needs draw.io",
)
def test_drawio_real(tmp_path: Path) -> None:
    source = tmp_path / "box.drawio"
    source.write_text(
        '<mxfile><diagram name="p"><mxGraphModel><root><mxCell id="0"/>'
        '<mxCell id="1" parent="0"/><mxCell id="2" value="Box" vertex="1" parent="1">'
        '<mxGeometry x="10" y="10" width="80" height="40" as="geometry"/></mxCell>'
        "</root></mxGraphModel></diagram></mxfile>"
    )
    target = tmp_path / "box.png"
    diagrams.render_drawio(source, target)
    assert target.read_bytes()[:4] == b"\x89PNG"


@needs_pandoc
@pytest.mark.skipif(shutil.which("typst") is None, reason="needs typst")
def test_pdf_and_pptx(packs: Workspace) -> None:
    source = packs.paths.out / "assessment.md"
    source.write_text(DOC)
    pdf = render_document(packs, source, "pdf")
    assert pdf.output.read_bytes()[:4] == b"%PDF"
    deck = render_document(packs, source, "pptx", template="clientA")
    assert deck.kind in {"report", "deck"}
    from pptx import Presentation

    assert len(Presentation(str(deck.output)).slides) >= 2


@needs_pandoc
def test_toc_prefill_without_libreoffice(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dossier.ops import tools

    real = tools.tool

    def no_soffice(key: str):  # type: ignore[no-untyped-def]
        found = real(key)
        if key == "soffice":
            return type(found)(key, (), found.features, found.purpose)
        return found

    monkeypatch.setattr("dossier.docs.render.tool", no_soffice)
    source = tmp_path / "doc.md"
    source.write_text(DOC)
    result = render_document(None, source, "docx", out=tmp_path / "doc.docx")
    assert any("TOC page numbers" in w for w in result.warnings)
    text = _all_text(result.output)
    assert "Summary" in text and "w:updateFields" in text


@needs_pandoc
def test_mcp_render_tool_only_when_allowed(packs: Workspace) -> None:
    from mcp import Client

    from dossier.agent.mcp_server import build_server

    async def tools(ws: Workspace) -> set[str]:
        async with Client(build_server(ws.root)) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert "templates" in asyncio.run(tools(packs))
    assert "render_document" not in asyncio.run(tools(packs))
    config = packs.paths.config
    config.write_text(config.read_text().replace("allow_render = false", "allow_render = true"))
    allowed = open_workspace(packs.root)
    (allowed.paths.out / "memo.md").write_text("# Memo\n\nHello.\n")

    async def render() -> object:
        async with Client(build_server(allowed.root)) as client:
            return (
                await client.call_tool("render_document", {"markdown_path": "out/memo.md"})
            ).structured_content

    data = asyncio.run(render())
    assert str(data["output"]).endswith("out/memo.docx")  # type: ignore[index]
