# Installing dossier

dossier is a Python package installed with [uv](https://docs.astral.sh/uv/). It
needs a few system programs for specific features; `dsr doctor` lists what is
present and how to add what is missing.

## macOS (Apple Silicon)

```bash
brew bundle --file Brewfile
uv tool install --python 3.12 "<path-or-url-to-dossier>[all]"
dsr doctor
```

| Program | Needed for | Homebrew |
|---|---|---|
| uv | installing dossier | `brew install uv` |
| ollama | local models (chat + embeddings) | `brew install ollama` |
| ffmpeg | meetings | `brew install ffmpeg` |
| pandoc, typst | DOCX and PDF output | `brew install pandoc typst` |
| mmdc | Mermaid diagrams | `brew install mermaid-cli` |
| draw.io | draw.io diagrams (optional) | `brew install --cask drawio` |
| LibreOffice | DOCX to PDF, slide rendering (optional) | `brew install --cask libreoffice` |
| bun | qmd search backend (optional) | `brew install bun` |

Start the model server with `ollama serve` (or `brew services start ollama` to keep
it running), then pull the models your workspace names with `dsr setup --with llm`.

## Python extras

| Extra | Adds |
|---|---|
| `ocr` | Apple Vision OCR (macOS) |
| `embed` | vector search (`sqlite-vec`) |
| `mcp` | the MCP server |
| `cloud` | Anthropic / OpenAI providers (still subject to workspace policy) |
| `all` | all of the above |

Add extras later with `uv tool install --reinstall --python 3.12 "dossier[...]"`.

## Upgrading

```bash
uv tool upgrade dossier
```

## Windows

Planned. See [windows-port.md](windows-port.md).
