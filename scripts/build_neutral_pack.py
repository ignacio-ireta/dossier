"""Build the built-in "neutral" template pack from scratch.

Starts from python-docx / python-pptx's own default templates (MIT) and adds
every style pandoc's DOCX writer uses, a bordered "Table" style, a running
header with a {{title}} token, a page-number footer, and a 16:9 deck. Run from
the repository root:

    uv run python scripts/build_neutral_pack.py
"""

from __future__ import annotations

import copy
from pathlib import Path

import docx
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from docx.shared import Pt, RGBColor
from pptx import Presentation
from pptx.util import Emu

PACK = Path(__file__).resolve().parents[1] / "src" / "dossier" / "docs" / "packs" / "neutral"
ACCENT = RGBColor(0x1F, 0x38, 0x64)
FONT = "Calibri"


def _paragraph_style(doc: docx.document.Document, name: str, base: str = "Body Text") -> object:
    styles = doc.styles
    try:
        return styles[name]
    except KeyError:
        style = styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        style.base_style = styles[base]
        return style


def build_docx(target: Path) -> None:
    doc = docx.Document()
    styles = doc.styles
    normal = styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(11)
    body = styles["Body Text"]
    body.paragraph_format.space_after = Pt(8)
    body.paragraph_format.line_spacing = 1.15
    for level, size in ((1, 18), (2, 14), (3, 12), (4, 11)):
        heading = styles[f"Heading {level}"]
        heading.font.name = FONT
        heading.font.size = Pt(size)
        heading.font.bold = True
        heading.font.color.rgb = ACCENT
        heading.paragraph_format.space_before = Pt(14 if level == 1 else 10)
        heading.paragraph_format.space_after = Pt(4)
    title = styles["Title"]
    title.font.name = FONT
    title.font.size = Pt(28)
    title.font.color.rgb = ACCENT
    subtitle = styles["Subtitle"]
    subtitle.font.size = Pt(14)
    for name in (
        "First Paragraph",
        "Compact",
        "Block Text",
        "Abstract",
        "Definition",
        "Definition Term",
        "Footnote Text",
        "Bibliography",
    ):
        _paragraph_style(doc, name)
    styles["Compact"].paragraph_format.space_after = Pt(2)  # type: ignore[attr-defined]
    for name in ("Author", "Date"):
        style = _paragraph_style(doc, name)
        style.paragraph_format.alignment = 1  # type: ignore[attr-defined]
        style.font.size = Pt(12)  # type: ignore[attr-defined]
    for name in ("Image Caption", "Table Caption"):
        style = _paragraph_style(doc, name, base="Caption")
        style.font.italic = True  # type: ignore[attr-defined]
    for name in ("Figure", "Captioned Figure"):
        _paragraph_style(doc, name, base="Normal")
    source = _paragraph_style(doc, "Source Code", base="Normal")
    source.font.name = "Menlo"  # type: ignore[attr-defined]
    source.font.size = Pt(9.5)  # type: ignore[attr-defined]
    for name, font in (("Verbatim Char", "Menlo"), ("Hyperlink", None)):
        try:
            styles[name]
        except KeyError:
            char = styles.add_style(name, WD_STYLE_TYPE.CHARACTER)
            if font:
                char.font.name = font
            else:
                char.font.color.rgb = RGBColor(0x1F, 0x5F, 0xBF)
                char.font.underline = True
    table_xml = (
        f'<w:style {nsdecls("w")} w:type="table" w:styleId="Table">'
        '<w:name w:val="Table"/><w:basedOn w:val="TableNormal"/><w:uiPriority w:val="99"/>'
        '<w:pPr><w:spacing w:before="40" w:after="40"/></w:pPr>'
        "<w:tblPr><w:tblBorders>"
        '<w:top w:val="single" w:sz="4" w:color="8EA9C1"/>'
        '<w:bottom w:val="single" w:sz="4" w:color="8EA9C1"/>'
        '<w:insideH w:val="single" w:sz="4" w:color="BFCFDD"/>'
        '</w:tblBorders><w:tblCellMar><w:left w:w="80" w:type="dxa"/>'
        '<w:right w:w="80" w:type="dxa"/></w:tblCellMar></w:tblPr>'
        '<w:tblStylePr w:type="firstRow"><w:rPr><w:b/><w:color w:val="FFFFFF"/></w:rPr>'
        '<w:tcPr><w:shd w:val="clear" w:color="auto" w:fill="1F3864"/></w:tcPr></w:tblStylePr>'
        "</w:style>"
    )
    styles.element.append(parse_xml(table_xml))
    section = doc.sections[0]
    header = section.header.paragraphs[0]
    header.text = "{{title}}"
    header.style = styles["Header"]
    footer = section.footer.paragraphs[0]
    footer.alignment = 2
    footer._p.append(
        parse_xml(
            f'<w:fldSimple {nsdecls("w")} w:instr="PAGE"><w:r><w:t>1</w:t></w:r></w:fldSimple>'
        )
    )
    core = doc.core_properties
    core.author = "dossier"
    core.last_modified_by = "dossier"
    core.title = "neutral reference"
    core.comments = ""
    for paragraph in list(doc.paragraphs):
        paragraph._p.getparent().remove(paragraph._p)
    target.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(target))


def _scale_shapes(shapes: object, factor: float) -> None:
    """Scale only shapes with their own position; inheriting placeholders follow the master."""
    for shape in shapes:  # type: ignore[attr-defined]
        own = shape._element.xpath("./*[local-name()='spPr']/*[local-name()='xfrm']")
        if own and shape.left is not None and shape.width is not None:
            shape.left = Emu(int(shape.left * factor))
            shape.width = Emu(int(shape.width * factor))


def build_pptx(target: Path) -> None:
    deck = Presentation()
    old_width = deck.slide_width
    deck.slide_width = Emu(12_192_000)  # 13.333 in: 16:9
    factor = deck.slide_width / old_width
    master = deck.slide_master
    _scale_shapes(master.shapes, factor)
    for layout in deck.slide_layouts:
        _scale_shapes(layout.shapes, factor)
    for shape in master.shapes:
        if shape.has_text_frame:
            for paragraph in shape.text_frame.paragraphs:
                for run in paragraph.runs:
                    run.font.name = FONT
    title_style = master.element.find(".//{*}titleStyle")
    if title_style is not None:
        for props in title_style.iter(
            "{http://schemas.openxmlformats.org/drawingml/2006/main}defRPr"
        ):
            fill = copy.deepcopy(
                parse_xml(
                    '<a:solidFill xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                    '<a:srgbClr val="1F3864"/></a:solidFill>'
                )
            )
            props.insert(0, fill)
    core = deck.core_properties
    core.author = "dossier"
    core.last_modified_by = "dossier"
    core.title = "neutral deck"
    target.parent.mkdir(parents=True, exist_ok=True)
    deck.save(str(target))


THEME = """// Neutral PDF theme for dossier (pandoc's typst writer imports `conf`).
#let accent = rgb("#1F3864")

#let conf(
  title: none, subtitle: none, authors: (), keywords: (), date: none,
  abstract-title: none, abstract: none, thanks: none, cols: 1,
  margin: (x: 2.2cm, y: 2.4cm), paper: "a4", lang: "en", region: "US",
  font: none, fontsize: 10.5pt, mathfont: none, codefont: none, linestretch: 1,
  sectionnumbering: none, linkcolor: none, citecolor: none, filecolor: none,
  pagenumbering: "1", doc,
) = {
  set document(title: title, keywords: keywords)
  set page(
    paper: paper, margin: margin, numbering: pagenumbering, columns: cols,
    header: context {
      if counter(page).get().first() > 1 and title != none [
        #set text(size: 8.5pt, fill: luma(110))
        #title #h(1fr) #if date != none { date }
      ]
    },
  )
  set par(justify: true, leading: linestretch * 0.65em)
  set text(lang: lang, region: region, size: fontsize)
  set text(font: font) if font != none
  show raw: set text(font: codefont) if codefont != none
  set heading(numbering: sectionnumbering)
  show heading: set text(fill: accent)
  show heading.where(level: 1): it => block(above: 1.4em, below: 0.7em, text(size: 1.45em, it))
  show link: set text(fill: rgb("#1F5FBF"))
  set table(
    stroke: (x, y) => if y == 0 { (bottom: 0.8pt + accent) } else { (bottom: 0.4pt + luma(200)) },
    fill: (x, y) => if y == 0 { accent.lighten(85%) },
  )
  if title != none {
    block(below: 1.6em, width: 100%)[
      #text(size: 2em, weight: "bold", fill: accent, hyphenate: false)[#title]
      #if subtitle != none { parbreak(); text(size: 1.2em, fill: luma(80))[#subtitle] }
      #if authors != none and authors != () {
        parbreak()
        text(fill: luma(60))[#authors.map(a => a.name).join(", ")]
      }
      #if date != none { h(1em); text(fill: luma(100))[#date] }
    ]
  }
  doc
}
"""

PACK_TOML = """# Built-in neutral pack. Copy it with `dsr templates new <name> --extends neutral`.
name = "neutral"
version = "1"
description = "Plain, brand-free documents and decks."
default_kind = "report"

[kinds.report]
docx = "report/reference.docx"
pptx = "deck/template.pptx"
pdf_theme = "pdf/theme.typ"
toc = true

[kinds.memo]
docx = "report/reference.docx"
pdf_theme = "pdf/theme.typ"
toc = false

[kinds.deck]
pptx = "deck/template.pptx"
"""


def main() -> None:
    build_docx(PACK / "report" / "reference.docx")
    build_pptx(PACK / "deck" / "template.pptx")
    (PACK / "pdf").mkdir(parents=True, exist_ok=True)
    (PACK / "pdf" / "theme.typ").write_text(THEME, encoding="utf-8")
    (PACK / "pack.toml").write_text(PACK_TOML, encoding="utf-8")
    print(f"built {PACK}")


if __name__ == "__main__":
    main()
