from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from dossier.convert.discovery import discover, glob_regex
from dossier.convert.extractors import ExtractContext, ExtractionError
from dossier.convert.extractors.mail import MsgExtractor
from dossier.convert.extractors.pdf import ImageExtractor, PdfExtractor
from dossier.convert.extractors.web import html_to_blocks
from dossier.convert.model import Heading, ListItem, Table
from dossier.convert.snapshot import snapshot_path
from dossier.core.config import OcrMode, SourceRoot
from dossier.core.errors import PolicyBlockedError
from dossier.core.policy import set_processing
from dossier.core.workspace import Workspace, open_workspace
from dossier.eval.synthetic import build_corpus, minimal_pdf
from dossier.ops.sync import SyncOptions, run_sync
from dossier.platform.fake import FakeState


@pytest.fixture
def approved(workspace: Workspace) -> Workspace:
    from dossier.core.config import ProcessingStatus

    set_processing(workspace.paths.config, ProcessingStatus.APPROVED)
    build_corpus(workspace.paths.sources)
    return open_workspace(workspace.root)


def _convert(ws: Workspace, **options: object) -> dict[str, object]:
    outcome = run_sync(ws, SyncOptions(stages=["convert"], **options))  # type: ignore[arg-type]
    assert outcome.exit_status in (0, 75), outcome.stages
    return outcome.stages["convert"]["main"]  # type: ignore[no-any-return]


def _corpus(ws: Workspace, relative: str) -> str:
    return (ws.paths.corpus / "main" / relative).read_text(encoding="utf-8")


def test_policy_gate_blocks_conversion(workspace: Workspace) -> None:
    build_corpus(workspace.paths.sources)
    with pytest.raises(PolicyBlockedError):
        run_sync(workspace, SyncOptions(stages=["convert"]))
    assert not any(workspace.paths.corpus.iterdir())


def test_full_conversion_outputs(approved: Workspace) -> None:
    result = _convert(approved)
    counts = result["counts"]  # type: ignore[index]
    assert counts["added"] == 9 and counts["failed"] == 0
    charter = _corpus(approved, "plans/project-charter.docx.md")
    assert 'source: "main/plans/project-charter.docx"' in charter
    assert "1.2 million" in charter and "- Ingestion of sales data" in charter
    assert "| Role | Team |" in charter
    roadmap = _corpus(approved, "plans/roadmap.pptx.md")
    assert "## Slide 2: Phases" in roadmap and "  - Includes the finance team" in roadmap
    assert "## Speaker notes, slide 2" in roadmap
    budget = _corpus(approved, "data/budget.xlsx.md")
    assert "Full data: `corpus/main/data/budget.xlsx.sheets/Budget.csv`" in budget
    csv_text = (approved.paths.corpus / "main/data/budget.xlsx.sheets/Budget.csv").read_text()
    assert csv_text.splitlines()[1] == "Ingestion,340000,Platform"
    teams = (approved.paths.corpus / "main/data/teams.csv.sheets/teams.csv").read_text()
    assert teams.splitlines() == ["team,members", "Platform,6", "Analytics,4"]
    pdf = _corpus(approved, "reports/risk-review.pdf.md")
    assert "### Quarterly Risk Review" in pdf and "vendor lock-in" in pdf
    assert "| Vendor lock-in | High |" in pdf
    mail = _corpus(approved, "mail/steering-decision.eml.md")
    assert mail.startswith("---") and "Lakeside region" in mail and "hosting-options.pdf" in mail
    assert "**DPO**" in _corpus(approved, "notes/glossary.md.md")
    meta = json.loads(_corpus(approved, "data/budget.xlsx.meta.json"))
    assert [s["name"] for s in meta["sheets"]] == ["Budget", "Milestones"]
    assert meta["warnings"][0]["code"] == "FORMULA_NO_CACHED_VALUE"


def test_incremental_runs(approved: Workspace) -> None:
    _convert(approved)
    first = json.loads(snapshot_path(approved.paths, "main").read_text())
    second = _convert(approved)
    assert second["counts"]["unchanged"] == 9  # type: ignore[index]
    assert second["counts"]["added"] == 0  # type: ignore[index]

    sources = approved.paths.sources
    glossary = sources / "notes" / "glossary.md"
    glossary.write_text(glossary.read_text() + "- **ETL**: extract, transform, load.\n")
    shutil.move(str(sources / "plans" / "roadmap.pptx"), str(sources / "plans" / "roadmap-v2.pptx"))
    (sources / "notes" / "settings.json").unlink()
    third = _convert(approved)
    counts = third["counts"]  # type: ignore[index]
    assert (counts["updated"], counts["moved"], counts["deleted"]) == (1, 1, 1)
    assert "ETL" in _corpus(approved, "notes/glossary.md.md")
    moved = _corpus(approved, "plans/roadmap-v2.pptx.md")
    assert 'source: "main/plans/roadmap-v2.pptx"' in moved
    assert not (approved.paths.corpus / "main/plans/roadmap.pptx.md").exists()
    archived = list((approved.paths.state / "archive").rglob("settings.json.md"))
    assert len(archived) == 1
    state = json.loads(snapshot_path(approved.paths, "main").read_text())
    assert "plans/roadmap-v2.pptx" in state["files"]
    assert (
        state["files"]["data/budget.xlsx"]["sha256"] == first["files"]["data/budget.xlsx"]["sha256"]
    )


def test_mass_delete_is_blocked(approved: Workspace) -> None:
    _convert(approved)
    shutil.rmtree(approved.paths.sources)
    approved.paths.sources.mkdir()
    blocked = run_sync(approved, SyncOptions(stages=["convert"]))
    assert blocked.exit_status == 75
    assert "empty" in (blocked.stages["convert"]["main"]["blocked"] or "")
    assert (approved.paths.corpus / "main/plans/project-charter.docx.md").exists()
    confirmed = _convert(approved, confirm_deletes=True)
    assert confirmed["counts"]["deleted"] == 9  # type: ignore[index]


def test_missing_source_is_blocked_not_deleted(approved: Workspace) -> None:
    _convert(approved)
    shutil.rmtree(approved.paths.sources)
    outcome = run_sync(approved, SyncOptions(stages=["convert"]))
    assert outcome.exit_status == 75 and "missing" in outcome.attention[0]


def test_dry_run_writes_nothing(approved: Workspace) -> None:
    outcome = run_sync(approved, SyncOptions(stages=["convert"], dry_run=True))
    assert outcome.stages["convert"]["main"]["counts"]["added"] == 9
    assert not any(approved.paths.corpus.iterdir())
    assert not snapshot_path(approved.paths, "main").exists()
    assert not any(approved.paths.runs.iterdir())


def test_failed_file_is_recorded_and_others_continue(approved: Workspace) -> None:
    (approved.paths.sources / "broken.docx").write_bytes(b"not a zip file")
    result = _convert(approved)
    assert result["counts"]["failed"] == 1  # type: ignore[index]
    assert result["errors"][0]["code"] == "corrupt_file"  # type: ignore[index]
    errors = json.loads(
        (approved.paths.state / "state/convert/main.errors.json").read_text(encoding="utf-8")
    )
    assert errors == [{"path": "broken.docx", "code": "corrupt_file"}]
    again = _convert(approved)
    assert again["counts"]["failed"] == 0 and again["counts"]["unchanged"] == 10  # type: ignore[index]


def test_discovery_rules(tmp_path: Path, fake_platform: FakeState) -> None:
    root = tmp_path / "src"
    (root / ".hidden").mkdir(parents=True)
    (root / ".hidden" / "x.md").write_text("x")
    (root / "~$lock.docx").write_text("x")
    (root / "a.md").write_text("a")
    (root / "b.xyz").write_text("b")
    (root / "call.m4a").write_bytes(b"\x00")
    (root / "drafts").mkdir()
    (root / "drafts" / "c.md").write_text("c")
    cloud = root / "cloud.md"
    cloud.write_text("stub")
    fake_platform.placeholders.add(cloud)
    found = discover(root, SourceRoot(name="m", path=".", exclude=["drafts/**"]), max_bytes=10**6)
    assert [c.relpath for c in found.documents] == ["a.md"]
    assert [c.relpath for c in found.media] == ["call.m4a"]
    assert found.skipped == {
        "hidden_or_temp": 1,
        "unsupported_type": 1,
        "excluded": 1,
        "cloud_placeholder": 1,
    }
    materialized = discover(root, SourceRoot(name="m", path=".", materialize=True), max_bytes=10)
    assert "cloud.md" in [c.relpath for c in materialized.documents]


def test_glob_translation() -> None:
    assert glob_regex("**/*.md").match("a/b/c.md") and glob_regex("**/*.md").match("c.md")
    assert not glob_regex("*.md").match("a/c.md")
    assert glob_regex("drafts/**").match("drafts/x/y.docx")


def test_scanned_pdf_uses_platform_ocr(
    tmp_path: Path, fake_platform: FakeState, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dossier.convert.model import Paragraph

    path = tmp_path / "scan.pdf"
    minimal_pdf(path, [])  # no text layer

    def fake_ocr_pages(path: Path, pages: list[int], sections, warnings, ctx) -> None:  # type: ignore[no-untyped-def]
        sections[0].blocks = [Paragraph(text="recognized text")]

    monkeypatch.setattr(PdfExtractor, "_ocr_pages", staticmethod(fake_ocr_pages))
    # the minimal page has no images, so force mode is what routes it to OCR
    result = PdfExtractor().extract(path, ExtractContext(ocr=OcrMode.FORCE))
    assert result.document.sections[0].blocks[0].text == "recognized text"  # type: ignore[union-attr]


def test_image_ocr_through_platform(tmp_path: Path, fake_platform: FakeState) -> None:
    image = tmp_path / "whiteboard.png"
    image.write_bytes(b"\x89PNG")
    fake_platform.ocr_text[image] = ["Decision: weekly sync", "Owner: platform team"]
    result = ImageExtractor().extract(image, ExtractContext())
    texts = [b.text for b in result.document.sections[0].blocks]  # type: ignore[union-attr]
    assert texts == ["Decision: weekly sync", "Owner: platform team"]  # no boxes: one per line
    off = ImageExtractor().extract(image, ExtractContext(ocr=OcrMode.OFF))
    assert off.warnings[0].code == "OCR_OFF"


def test_corrupt_pdf_is_extraction_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 garbage")
    with pytest.raises(ExtractionError):
        PdfExtractor().extract(bad, ExtractContext())


def test_html_reader() -> None:
    title, blocks = html_to_blocks(
        "<html><head><title>T</title><style>p{}</style></head><body><h2>Head</h2>"
        "<ol><li>one</li><li>two</li></ol><script>x()</script>"
        "<table><tr><td>a</td><td>b</td></tr></table></body></html>"
    )
    assert title == "T"
    assert isinstance(blocks[0], Heading) and blocks[0].level == 2
    assert isinstance(blocks[1], ListItem) and blocks[1].ordered
    assert isinstance(blocks[-1], Table) and blocks[-1].rows == [["a", "b"]]


class _FakeOle:
    def __init__(self, streams: dict[str, bytes]) -> None:
        self.streams = streams

    def exists(self, name: str) -> bool:
        return name in self.streams

    def openstream(self, name: str):  # type: ignore[no-untyped-def]
        import io

        return io.BytesIO(self.streams[name])


def test_msg_property_readers() -> None:
    import struct

    ole = _FakeOle(
        {
            "__substg1.0_0037001F": "Weekly update".encode("utf-16-le"),
            "__substg1.0_0E04001E": b"Platform Team",
            "__properties_version1.0": b"\x00" * 32
            + struct.pack("<IIQ", 0x00390040, 0, 133_700_000_000_000_000),
        }
    )
    assert MsgExtractor._string(ole, "", "0037") == "Weekly update"
    assert MsgExtractor._string(ole, "", "0E04") == "Platform Team"
    assert MsgExtractor._date(ole).startswith("2024-")


def test_non_msg_file_rejected(tmp_path: Path) -> None:
    path = tmp_path / "x.msg"
    path.write_text("plain text")
    with pytest.raises(ExtractionError):
        MsgExtractor().extract(path, ExtractContext())


def test_mtime_shortcut_and_rehash(approved: Workspace) -> None:
    _convert(approved)
    target = approved.paths.sources / "notes" / "glossary.md"
    stat = target.stat()
    target.write_text(target.read_text().replace("DPO", "DPX"))
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # same size, same mtime
    assert _convert(approved)["counts"]["updated"] == 0  # type: ignore[index]
    assert _convert(approved, rehash=True)["counts"]["updated"] == 1  # type: ignore[index]


def test_ocr_lines_grouped_by_vertical_gap() -> None:
    from dossier.convert.ocr import group_into_paragraphs
    from dossier.platform.base import OcrLine

    lines = [
        OcrLine("second paragraph", 0.9, (0.1, 0.50, 0.5, 0.03)),
        OcrLine("first line", 0.9, (0.1, 0.10, 0.5, 0.03)),
        OcrLine("continues here", 0.9, (0.1, 0.135, 0.5, 0.03)),
    ]
    assert group_into_paragraphs(lines) == ["first line continues here", "second paragraph"]


@pytest.mark.skipif(
    __import__("sys").platform != "darwin"
    or __import__("importlib.util").util.find_spec("ocrmac") is None,
    reason="Apple Vision OCR",
)
def test_real_ocr_on_scanned_pdf(tmp_path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    from dossier import platform

    platform.use(None)  # the real macOS backend
    image = Image.new("RGB", (1400, 300), "white")
    ImageDraw.Draw(image).text(
        (40, 100), "Scanned budget memo", fill="black", font=ImageFont.load_default(size=56)
    )
    pdf = tmp_path / "scan.pdf"
    image.save(pdf, "PDF", resolution=100)
    result = PdfExtractor().extract(pdf, ExtractContext())
    text = " ".join(b.text for s in result.document.sections for b in s.blocks)  # type: ignore[union-attr]
    assert "budget memo" in text.lower()
    assert any(w.code == "OCR_APPLIED" for w in result.warnings)
