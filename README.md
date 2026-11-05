# dossier

dossier gives AI agents the same project context a person on the team has.

Point it at an account's folders and it:

- **keeps a corpus current**: every document converted to Markdown (and CSV for
  spreadsheets), re-scanned on a schedule, only changes processed;
- **summarizes and indexes it**: a card per document, a summary per folder, a map
  of the whole tree (`INDEX.md`, `TREE.md`), and hybrid keyword + semantic search;
- **processes meetings**: transcription, speaker separation, speaker-name
  suggestions, summaries, decisions and action items with checked timestamps;
- **renders deliverables**: DOCX, PPTX and PDF from Markdown, with Mermaid and
  draw.io diagrams, using each account's own template packs;
- **serves agents**: an MCP server, a CLI where every command has `--json`, and
  generated `AGENTS.md` / `llms.txt` files.

It is local-first. Everything runs on your machine with small local models such
as Gemma 4 E2B. Nothing is read until the workspace policy says so, and nothing
goes to an external service unless the policy names it.

## Install (macOS)

```bash
brew bundle --file Brewfile
uv tool install --python 3.12 "<path-or-url-to-dossier>[all]"
dsr doctor
```

Details: [docs/install.md](docs/install.md). Windows: [planned](docs/windows-port.md).

## Quick start

```bash
mkdir -p ~/Work/acme && cd ~/Work/acme
dsr init --name "Acme"                 # folders + dossier.toml (policy: pending)
dsr setup --with llm                   # local models through Ollama
dsr policy approve                     # once processing here is permitted
dsr sync                               # convert, transcribe, summarize, index
dsr search "which hosting option was chosen?"
dsr mcp install                        # connect Claude Code (prints the command)
```

## Documentation

| Guide | Covers |
|---|---|
| [Getting started](docs/getting-started.md) | Setting up a workspace for an account, end to end |
| [Configuration](docs/configuration.md) | Every `dossier.toml` setting |
| [Corpus](docs/corpus.md) | What each file type becomes; sync, safety, scheduling |
| [Index and search](docs/index.md) | Cards, folder summaries, search, qmd, benchmarks |
| [Meetings](docs/meetings.md) | Transcripts, speakers and voiceprints, summaries, glossary |
| [Documents](docs/documents.md) | Rendering, diagrams, per-account template packs |
| [Agents](docs/agents.md) | MCP server, CLI for agents, navigation files, skill |
| [Models](docs/models.md) | Roles, small-model design, other servers, cloud |
| [Install](docs/install.md) | System tools and Python extras |
| [Windows port](docs/windows-port.md) | What is portable, what remains |

## Commands

```text
dsr init | doctor | setup | status | policy | secrets
dsr sync | convert | index | schedule
dsr search | tree | read | taxonomy suggest | qmd sync
dsr meet add | list | show | speakers | summarize | brief | eval      dsr people ...
dsr docs render | templates list | show | inspect | new | import
dsr models list | test | bench
dsr mcp serve | install      dsr skill path | install
```

## Development

```bash
uv sync --all-extras
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
uv run python tests/e2e.py              # end to end with real tools and models
git config core.hooksPath .githooks     # denylist gate for personal/project data
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Contributions must not contain personal
data or names of clients, projects or people.

## License

MIT. See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md).
