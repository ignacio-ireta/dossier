# Installing dossier

dossier is a Python package installed with [uv](https://docs.astral.sh/uv/). It
uses a few system programs for specific features; `dsr doctor` shows what is
present and how to add what is missing.

## macOS (Apple Silicon)

```bash
brew bundle --file Brewfile
uv tool install --python 3.12 "<path-or-url-to-dossier>[all]"
dsr doctor
brew services start ollama            # or run `ollama serve` when needed
```

| Program | Needed for | Install |
|---|---|---|
| uv | installing dossier | `brew install uv` |
| ollama | local models (chat and embeddings) | `brew install ollama` |
| ffmpeg | meetings | `brew install ffmpeg` |
| pandoc, typst | DOCX, PPTX and PDF output | `brew install pandoc typst` |
| mmdc + a browser | Mermaid diagrams | `brew install mermaid-cli`; it uses puppeteer's `chrome-headless-shell` (`npx puppeteer browsers install chrome-headless-shell`) or an installed Chrome, Chromium, Edge or Brave |
| draw.io | draw.io diagrams (optional) | `brew install --cask drawio` |
| LibreOffice | TOC page numbers, PDF via DOCX, slide images (optional) | `brew install --cask libreoffice` |
| bun + qmd | qmd search backend (optional) | `brew install bun`, then `bun install -g @tobilu/qmd` (or `dsr setup --with qmd`) |

`dsr setup --with llm,docs,meet,qmd` shows the missing pieces for those
features and, after confirmation, installs them with Homebrew and pulls the
models the workspace names.

## Python extras

| Extra | Adds |
|---|---|
| `ocr` | Apple Vision OCR (macOS) |
| `embed` | vector search (`sqlite-vec`) |
| `mcp` | the MCP server |
| `meet` | meetings: Whisper (MLX on Apple Silicon, faster-whisper elsewhere) and pyannote |
| `cloud` | Anthropic / OpenAI providers (still subject to the workspace policy) |
| `all` | all of the above (about 2 GB, most of it PyTorch) |

Add extras later with `uv tool install --reinstall --python 3.12 "dossier[...]"`.

## Speaker diarization model

Accept the terms of `pyannote/speaker-diarization-community-1` on Hugging Face,
create a read token, and store it with `dsr secrets set hf-token`.

## Upgrading and removing

```bash
uv tool upgrade dossier
dsr schedule remove        # in each workspace with a schedule, before uninstalling
uv tool uninstall dossier
```

Workspaces are plain folders; removing dossier leaves them untouched.

## Windows

Planned. See [windows-port.md](windows-port.md).
