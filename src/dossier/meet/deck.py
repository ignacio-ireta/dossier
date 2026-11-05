"""Slides shown in a meeting: their text, and optional captions for visual slides.

The deck's text comes from the same extractors as the corpus. If the ``vision``
role is enabled and LibreOffice is installed, each slide is rendered and an
image-capable model describes what is visible (diagrams, charts) in one short
paragraph, so a summary can refer to them.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from dossier.convert.extractors import ExtractContext, extractor_for
from dossier.convert.markdown import render
from dossier.convert.normalize import normalize
from dossier.core.errors import DossierError
from dossier.llm.router import RoleClient
from dossier.llm.structured import Outcome, generate_structured
from dossier.ops.tools import tool


class _Caption(BaseModel):
    caption: str = Field(max_length=500)


def deck_markdown(path: Path) -> str:
    extractor = extractor_for(path)
    if extractor is None or extractor.name not in {"pptx", "pdf"}:
        raise DossierError(f"{path.name}: decks must be .pptx or .pdf")
    result = extractor.extract(path, ExtractContext())
    return render(normalize(result.document), file_name=path.name, meta={"deck": path.name})


def render_slides(path: Path, out_dir: Path) -> list[Path]:
    """Slides as PNG files (pptx via LibreOffice to PDF, then pypdfium2)."""
    import pypdfium2 as pdfium

    pdf = path
    with tempfile.TemporaryDirectory(prefix="dossier-deck-") as tmp:
        if path.suffix.lower() != ".pdf":
            soffice = tool("soffice").locate()
            if soffice is None:
                raise DossierError(
                    "rendering slides needs LibreOffice (brew install --cask libreoffice)"
                )
            subprocess.run(
                [str(soffice), "--headless", "--convert-to", "pdf", "--outdir", tmp, str(path)],
                check=True,
                capture_output=True,
                timeout=600,
            )
            pdf = Path(tmp) / f"{path.stem}.pdf"
        document = pdfium.PdfDocument(str(pdf))
        out_dir.mkdir(parents=True, exist_ok=True)
        images = []
        try:
            for index in range(len(document)):
                target = out_dir / f"slide-{index + 1:03d}.png"
                document[index].render(scale=1.5).to_pil().save(target)
                images.append(target)
        finally:
            document.close()
    return images


def caption_slides(client: RoleClient, images: list[Path]) -> list[str]:
    captions = []
    for number, image in enumerate(images, start=1):
        result = generate_structured(
            client,
            system="You describe presentation slides. Describe only what is visible.",
            user=f"Slide {number}: describe the diagrams, charts or tables in 1-3 sentences.",
            schema=_Caption,
            fallback=lambda: _Caption(caption=""),
            images=(image.read_bytes(),),
        )
        captions.append(result.value.caption if result.outcome is not Outcome.DEGRADED else "")
    return captions


def has_libreoffice() -> bool:
    return tool("soffice").locate() is not None or shutil.which("soffice") is not None
