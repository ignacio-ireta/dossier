"""Turn diagrams in Markdown into images before rendering.

- Mermaid code blocks (```` ```mermaid ````) are rendered with ``mmdc``.
- draw.io diagrams, either a ```` ```drawio ```` block holding the diagram XML or
  an image link to a ``.drawio`` file (``![Flow](flow.drawio)``), are exported
  with the draw.io desktop app's command line.

A first line ``%% caption: <text>`` (Mermaid) sets the figure caption. Images go
to a cache keyed by content hash, so unchanged diagrams are not re-rendered.
PNG is used for DOCX/PPTX, SVG for PDF.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dossier.core.errors import ExternalToolError
from dossier.core.io import sha256_text
from dossier.ops.tools import tool

_FENCE = re.compile(
    r"^```[ \t]*(mermaid|drawio)[ \t]*\n(.*?)\n```[ \t]*$", re.MULTILINE | re.DOTALL
)
_DRAWIO_LINK = re.compile(r"!\[([^\]]*)\]\(([^)\s]+\.drawio)\)")
_CAPTION = re.compile(r"^%%\s*caption:\s*(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class Diagram:
    kind: str
    output: Path
    cached: bool


def _run(command: list[str], what: str) -> None:
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=300)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:300]
        raise ExternalToolError(f"{what} failed: {detail}")


def _mmdc() -> str:
    found = tool("mmdc").locate()
    if found is None:
        raise ExternalToolError(f"Mermaid diagrams need mmdc: {tool('mmdc').install_hint()}")
    return str(found)


def _drawio() -> str:
    found = tool("drawio").locate()
    if found is None:
        raise ExternalToolError(f"draw.io diagrams need draw.io: {tool('drawio').install_hint()}")
    return str(found)


def browser() -> Path | None:
    """A Chromium-family browser for mmdc's puppeteer.

    Order: $PUPPETEER_EXECUTABLE_PATH, puppeteer's own cached headless shell (newest),
    then an installed Chrome / Chromium / Edge / Brave.
    """
    env = os.environ.get("PUPPETEER_EXECUTABLE_PATH")
    if env and Path(env).is_file():
        return Path(env)
    cache = Path.home() / ".cache" / "puppeteer"
    shells = sorted(
        cache.glob("chrome-headless-shell/*/chrome-headless-shell-*/chrome-headless-shell")
    )
    shells += sorted(
        cache.glob("chrome-headless-shell/*/chrome-headless-shell-*/chrome-headless-shell.exe")
    )
    if shells:
        return shells[-1]
    return tool("browser").locate()


def render_mermaid(source: str, target: Path) -> None:
    work = target.with_suffix(".mmd")
    work.write_text(source, encoding="utf-8")
    command = [_mmdc(), "-i", str(work), "-o", str(target), "-b", "white", "--quiet"]
    executable = browser()
    if executable is not None:
        config = target.with_suffix(".puppeteer.json")
        config.write_text(
            json.dumps({"executablePath": str(executable), "args": ["--no-sandbox"]}),
            encoding="utf-8",
        )
        command += ["-p", str(config)]
    # Plain SVG text instead of HTML labels: Typst and Word cannot show foreignObject.
    settings = target.with_suffix(".mermaid.json")
    settings.write_text(
        json.dumps({"htmlLabels": False, "flowchart": {"htmlLabels": False}}), encoding="utf-8"
    )
    command += ["-c", str(settings)]
    if target.suffix == ".png":
        command += ["-s", "2"]
    try:
        _run(command, "Mermaid rendering")
    finally:
        work.unlink(missing_ok=True)
        target.with_suffix(".puppeteer.json").unlink(missing_ok=True)
        target.with_suffix(".mermaid.json").unlink(missing_ok=True)


def render_drawio(source: Path, target: Path) -> None:
    fmt = target.suffix.lstrip(".")
    command = [_drawio(), "--export", "--format", fmt, "--border", "10", "--output", str(target)]
    if fmt == "png":
        command += ["--scale", "2"]
    _run([*command, str(source)], "draw.io export")


def prerender(
    markdown: str, *, base_dir: Path, cache_dir: Path, fmt: str
) -> tuple[str, list[Diagram]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    diagrams: list[Diagram] = []

    def fence(match: re.Match[str]) -> str:
        kind, body = match.group(1), match.group(2)
        caption_match = _CAPTION.search(body) if kind == "mermaid" else None
        caption = caption_match.group(1).strip() if caption_match else ""
        target = cache_dir / f"{kind}-{sha256_text(kind + fmt + body)[:16]}.{fmt}"
        cached = target.is_file()
        if not cached:
            if kind == "mermaid":
                render_mermaid(body, target)
            else:
                xml = target.with_suffix(".drawio")
                xml.write_text(body, encoding="utf-8")
                render_drawio(xml, target)
        diagrams.append(Diagram(kind, target, cached))
        return f"![{caption}]({target})"

    def link(match: re.Match[str]) -> str:
        caption, reference = match.group(1), match.group(2)
        source = (base_dir / reference).resolve()
        if not source.is_file():
            raise ExternalToolError(f"draw.io file not found: {reference}")
        content = source.read_text(encoding="utf-8", errors="replace")
        target = cache_dir / f"drawio-{sha256_text(fmt + content)[:16]}.{fmt}"
        cached = target.is_file()
        if not cached:
            render_drawio(source, target)
        diagrams.append(Diagram("drawio", target, cached))
        return f"![{caption}]({target})"

    markdown = _FENCE.sub(fence, markdown)
    markdown = _DRAWIO_LINK.sub(link, markdown)
    return markdown, diagrams
