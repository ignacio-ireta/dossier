"""External programs dossier can use, which features need them, how to install them."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExternalTool:
    key: str
    commands: tuple[str, ...]
    features: tuple[str, ...]
    purpose: str
    brew: str | None = None
    cask: str | None = None
    other_install: str | None = None
    mac_app_paths: tuple[str, ...] = ()
    optional: bool = False

    def locate(self) -> Path | None:
        for command in self.commands:
            found = shutil.which(command)
            if found:
                return Path(found)
        if sys.platform == "darwin":
            for candidate in self.mac_app_paths:
                path = Path(candidate)
                if path.is_file():
                    return path
        return None

    def install_hint(self) -> str:
        if sys.platform == "darwin":
            if self.cask:
                return f"brew install --cask {self.cask}"
            if self.brew:
                return f"brew install {self.brew}"
        if self.other_install:
            return self.other_install
        return "see docs/install.md"


TOOLS: tuple[ExternalTool, ...] = (
    ExternalTool("uv", ("uv",), ("install",), "Python package manager", brew="uv"),
    ExternalTool("ollama", ("ollama",), ("llm",), "local model server", brew="ollama"),
    ExternalTool("ffmpeg", ("ffmpeg",), ("meet",), "audio/video normalization", brew="ffmpeg"),
    ExternalTool("pandoc", ("pandoc",), ("docs",), "Markdown to DOCX/PDF", brew="pandoc"),
    ExternalTool("typst", ("typst",), ("docs",), "PDF engine", brew="typst"),
    ExternalTool(
        "mmdc",
        ("mmdc",),
        ("docs",),
        "Mermaid diagrams",
        brew="mermaid-cli",
        other_install="npm install -g @mermaid-js/mermaid-cli",
    ),
    ExternalTool(
        "drawio",
        ("drawio", "draw.io"),
        ("docs",),
        "draw.io diagram export",
        cask="drawio",
        mac_app_paths=("/Applications/draw.io.app/Contents/MacOS/draw.io",),
        optional=True,
    ),
    ExternalTool(
        "soffice",
        ("soffice", "libreoffice"),
        ("docs", "meet"),
        "DOCX to PDF, slide rendering, TOC page numbers",
        cask="libreoffice",
        mac_app_paths=("/Applications/LibreOffice.app/Contents/MacOS/soffice",),
        optional=True,
    ),
    ExternalTool("bun", ("bun",), ("qmd",), "runtime for qmd", brew="bun", optional=True),
)

FEATURES: dict[str, str] = {
    "llm": "local models through Ollama (chat + embeddings)",
    "docs": "render DOCX, PPTX and PDF with diagrams",
    "meet": "transcribe and summarize meetings",
    "ocr": "OCR for images and scanned PDFs",
    "qmd": "optional qmd search backend",
}


def tools_for(features: set[str]) -> list[ExternalTool]:
    return [tool for tool in TOOLS if set(tool.features) & features]


def tool(key: str) -> ExternalTool:
    for candidate in TOOLS:
        if candidate.key == key:
            return candidate
    raise KeyError(key)
