"""Generate a small synthetic project folder with planted facts.

Used by the test suite, the end-to-end check and ``dsr models bench``. Every
name, number and date here is invented; nothing describes a real organization.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path


@dataclass(frozen=True)
class Fact:
    """A fact planted in one file, with a question an agent should answer from it."""

    file: str
    question: str
    answer: str
    keywords: tuple[str, ...]


FACTS: tuple[Fact, ...] = (
    Fact(
        "plans/project-charter.docx",
        "What budget was approved for the Falcon data platform?",
        "1.2 million",
        ("budget", "falcon", "approved"),
    ),
    Fact(
        "plans/roadmap.pptx",
        "When does phase 2 of the rollout start?",
        "2027-03-01",
        ("phase 2", "rollout", "start"),
    ),
    Fact(
        "data/budget.xlsx",
        "What is the planned cost of the ingestion workstream?",
        "340000",
        ("ingestion", "cost"),
    ),
    Fact(
        "reports/risk-review.pdf",
        "Which risk was rated highest in the risk review?",
        "vendor lock-in",
        ("risk", "highest", "vendor"),
    ),
    Fact(
        "mail/steering-decision.eml",
        "Which hosting option did the steering committee choose?",
        "Lakeside region",
        ("steering", "hosting", "decision"),
    ),
    Fact(
        "notes/glossary.md",
        "What does the acronym DPO mean in this project?",
        "data product owner",
        ("glossary", "dpo"),
    ),
)


def _docx(path: Path) -> None:
    import docx

    document = docx.Document()
    document.core_properties.title = "Falcon Project Charter"
    document.add_heading("Falcon Project Charter", level=0)
    document.add_heading("Purpose", level=1)
    document.add_paragraph(
        "The Falcon data platform consolidates reporting for the operations teams. "
        "The steering committee approved a budget of 1.2 million for the first year."
    )
    document.add_heading("Scope", level=1)
    for item in ("Ingestion of sales data", "A shared metrics layer", "Self-service dashboards"):
        document.add_paragraph(item, style="List Bullet")
    table = document.add_table(rows=3, cols=2)
    for row, (role, owner) in enumerate(
        [("Role", "Team"), ("Sponsor", "Operations"), ("Delivery", "Platform team")]
    ):
        table.cell(row, 0).text = role
        table.cell(row, 1).text = owner
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))


def _pptx(path: Path) -> None:
    from pptx import Presentation

    deck = Presentation()
    title = deck.slides.add_slide(deck.slide_layouts[0])
    title.shapes.title.text = "Falcon Rollout Roadmap"
    title.placeholders[1].text = "Planning session"
    bullets = deck.slides.add_slide(deck.slide_layouts[1])
    bullets.shapes.title.text = "Phases"
    body = bullets.placeholders[1].text_frame
    body.text = "Phase 1: pilot with two teams"
    paragraph = body.add_paragraph()
    paragraph.text = "Phase 2 of the rollout starts on 2027-03-01"
    nested = body.add_paragraph()
    nested.text = "Includes the finance team"
    nested.level = 1
    bullets.notes_slide.notes_text_frame.text = "Confirm the phase 2 date with the sponsor."
    path.parent.mkdir(parents=True, exist_ok=True)
    deck.save(str(path))


def _xlsx(path: Path) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Budget"
    sheet.append(["Workstream", "Planned cost", "Owner team"])
    sheet.append(["Ingestion", 340000, "Platform"])
    sheet.append(["Metrics layer", 210000, "Analytics"])
    sheet.append(["Dashboards", 150000, "Analytics"])
    sheet.append(["Total", "=SUM(B2:B4)", ""])
    other = workbook.create_sheet("Milestones")
    other.append(["Milestone", "Date"])
    other.append(["Pilot", "2026-12-01"])
    other.append(["Phase 2", "2027-03-01"])
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(str(path))


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def minimal_pdf(
    path: Path,
    lines: Sequence[tuple[float, float, float, str]],
    rects: Sequence[tuple[float, float, float, float]] | None = None,
) -> None:
    """Write a one-page PDF: text lines (x, y, size, text) and stroked rectangles."""
    ops = []
    for x, y, w, h in rects or []:
        ops.append(f"{x} {y} {w} {h} re S")
    for x, y, size, text in lines:
        ops.append(f"BT /F1 {size} Tf {x} {y} Td ({_pdf_escape(text)}) Tj ET")
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))


def _pdf(path: Path) -> None:
    lines = [
        (72, 720, 22, "Quarterly Risk Review"),
        (72, 690, 11, "The review covered delivery, security and supplier risks."),
        (72, 676, 11, "The highest rated risk is vendor lock-in for the storage layer."),
        (72, 640, 15, "Mitigations"),
        (72, 618, 11, "Keep data in open formats and document an exit plan."),
        (78, 556, 11, "Risk"),
        (278, 556, 11, "Rating"),
        (78, 536, 11, "Vendor lock-in"),
        (278, 536, 11, "High"),
        (78, 516, 11, "Key person dependency"),
        (278, 516, 11, "Medium"),
    ]
    rects = [
        (72, 550, 200, 20),
        (272, 550, 120, 20),
        (72, 530, 200, 20),
        (272, 530, 120, 20),
        (72, 510, 200, 20),
        (272, 510, 120, 20),
    ]
    minimal_pdf(path, lines, rects)


def _eml(path: Path) -> None:
    message = EmailMessage()
    message["From"] = "Program Office <pmo@example.com>"
    message["To"] = "Platform Team <platform@example.com>"
    message["Subject"] = "Steering decision: hosting option"
    message["Date"] = "Mon, 02 Nov 2026 10:00:00 +0000"
    message.set_content(
        "Hello team,\n\nThe steering committee chose the Lakeside region as the hosting "
        "option for the Falcon platform.\n\nNext step: confirm network design by Friday.\n"
    )
    message.add_alternative(
        "<p>The steering committee chose the <b>Lakeside region</b>.</p>", subtype="html"
    )
    message.add_attachment(
        b"%PDF-1.4 stub", maintype="application", subtype="pdf", filename="hosting-options.pdf"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(message))


def build_corpus(root: Path) -> list[Fact]:
    """Write the synthetic files under ``root`` and return the planted facts."""
    _docx(root / "plans" / "project-charter.docx")
    _pptx(root / "plans" / "roadmap.pptx")
    _xlsx(root / "data" / "budget.xlsx")
    _pdf(root / "reports" / "risk-review.pdf")
    _eml(root / "mail" / "steering-decision.eml")
    notes = root / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "glossary.md").write_text(
        "# Glossary\n\n- **DPO**: data product owner, accountable for one data product.\n"
        "- **SLA**: service level agreement.\n",
        encoding="utf-8",
    )
    (notes / "status.html").write_text(
        "<html><head><title>Weekly status</title></head><body><h1>Status</h1>"
        "<p>Ingestion is on track.</p><ul><li>Pilot teams onboarded</li></ul>"
        "<table><tr><th>Item</th><th>State</th></tr><tr><td>Dashboards</td>"
        "<td>Started</td></tr></table></body></html>",
        encoding="utf-8",
    )
    (notes / "settings.json").write_text(
        json.dumps({"region": "lakeside", "tier": 2}), encoding="utf-8"
    )
    (root / "data" / "teams.csv").write_text(
        "team;members\nPlatform;6\nAnalytics;4\n", encoding="utf-8"
    )
    return list(FACTS)


# A short invented meeting between two people. Facts a summary should keep:
# the decision (Lakeside region), the action (migration checklist, owner Sam,
# due Friday) and the open question (budget increase approval).
MEETING_SCRIPT: tuple[tuple[int, str], ...] = (
    (0, "Good morning everyone. Let's review the Falcon rollout plan for this quarter."),
    (
        1,
        "Thanks. This is Sam from the platform team. The pilot finished last week and the "
        "two pilot teams are happy with the dashboards.",
    ),
    (0, "Great. We agreed that phase two of the rollout starts on the first of March."),
    (1, "I will prepare the migration checklist by Friday."),
    (0, "Perfect. One decision for the record: we keep the Lakeside region for hosting."),
    (1, "Agreed. One open question remains: who approves the budget increase for storage?"),
    (0, "Let's take that to the steering committee. Thanks everyone."),
)


# The same meeting in Spanish, for language detection and Spanish summaries.
MEETING_SCRIPT_ES: tuple[tuple[int, str], ...] = (
    (0, "Buenos días a todos. Vamos a revisar el plan de despliegue de Falcon de este trimestre."),
    (
        1,
        "Gracias. Soy Sam, del equipo de plataforma. El piloto terminó la semana pasada y los "
        "dos equipos piloto están contentos con los paneles.",
    ),
    (0, "Muy bien. Acordamos que la segunda fase del despliegue empieza el uno de marzo."),
    (1, "Yo prepararé la lista de verificación de la migración para el viernes."),
    (0, "Perfecto. Una decisión para el acta: mantenemos la región Lakeside para el alojamiento."),
    (
        1,
        "De acuerdo. Queda una pregunta abierta: ¿quién aprueba el aumento de presupuesto para "
        "el almacenamiento?",
    ),
    (0, "Lo llevamos al comité de dirección. Gracias a todos."),
)


FOLLOW_UP_SCRIPT: tuple[tuple[int, str], ...] = (
    (1, "Quick follow up on the checklist. Most of the items are done already."),
    (0, "Good to hear. Did we hear back about the storage budget?"),
    (1, "Not yet. The steering committee meets next Tuesday."),
    (0, "Fine. Please send me the checklist once it is complete."),
)


def build_meeting(
    path: Path,
    voices: tuple[str, str],
    *,
    script: tuple[tuple[int, str], ...] = MEETING_SCRIPT,
    pause_s: float = 0.7,
) -> list[str]:
    """Render MEETING_SCRIPT with two TTS voices into one audio file (needs ffmpeg).

    Returns the lines in order. Uses the platform speech-synthesis backend.
    """
    import shutil
    import subprocess
    import tempfile

    from dossier import platform

    tts = platform.current().tts
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required to build the synthetic meeting")
    with tempfile.TemporaryDirectory(prefix="dossier-meeting-") as tmp:
        parts: list[Path] = []
        for index, (speaker, line) in enumerate(script):
            part = Path(tmp) / f"{index:02d}.wav"
            tts.synthesize(line, part, voice=voices[speaker])
            parts.append(part)
        listing = Path(tmp) / "list.txt"
        silence = Path(tmp) / "silence.wav"
        subprocess.run(
            [
                ffmpeg,
                "-nostdin",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=16000:cl=mono",
                "-t",
                str(pause_s),
                "-c:a",
                "pcm_s16le",
                str(silence),
            ],
            check=True,
        )
        entries = []
        for part in parts:
            entries += [f"file '{part}'", f"file '{silence}'"]
        listing.write_text("\n".join(entries) + "\n", encoding="utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                ffmpeg,
                "-nostdin",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                str(path),
            ],
            check=True,
        )
    return [line for _, line in script]
