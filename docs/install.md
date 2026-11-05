# Installing dossier

dossier is a Python package installed with [uv](https://docs.astral.sh/uv/). It
uses a few system programs for specific features; `dsr doctor` shows what is
present and how to add what is missing.

## macOS (Apple Silicon)

You need 16 GB of memory or more and about 12 GB of free disk (8 GB for the
local models, 2 GB for Python packages).

From the shared bundle, `dossier-<version>-bundle.zip`:

```bash
unzip dossier-0.1.0-bundle.zip && cd dossier-0.1.0
shasum -a 256 -c SHA256SUMS            # optional: check the files arrived intact
brew bundle --file Brewfile
uv tool install --python 3.12 "dossier[all] @ ./dossier-0.1.0-py3-none-any.whl"
brew services start ollama            # or run `ollama serve` when needed
dsr doctor
```

From the source (a clone, or `git clone dossier-0.1.0.git.bundle dossier`):

```bash
brew bundle --file Brewfile
uv tool install --python 3.12 "<path-to-dossier>[all]"
dsr doctor
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

A newer bundle installs over the old version:

```bash
uv tool install --reinstall --python 3.12 "dossier[all] @ ./dossier-0.2.0-py3-none-any.whl"
```

To remove it:

```bash
dsr schedule remove        # first, in each workspace with a schedule
uv tool uninstall dossier
```

Workspaces are plain folders; removing dossier leaves them untouched.

## Windows

Planned. See [windows-port.md](windows-port.md).
