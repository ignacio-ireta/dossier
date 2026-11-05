# dossier

**Give AI agents the same project context your team has, on your own machine.**

dossier (command: `dsr`) turns an account's folders and meeting recordings into
material an agent can use, and turns an agent's Markdown into client-ready
documents. It runs locally with small models such as Gemma 4 E2B, behind a
policy gate that stays closed until you open it.

| | What you get |
|---|---|
| **Corpus** | Word, PowerPoint, Excel, PDF (with OCR), email and web files converted to Markdown and CSV, kept current on a schedule; only changes are processed. |
| **Index and search** | A summary card per document, a summary per folder, a map of the whole tree (`INDEX.md`, `TREE.md`), and keyword + semantic search. |
| **Meetings** | Transcripts in the language spoken (detected per recording), speakers named in a few minutes of listening and recognized later by voice (opt-in), summaries with decisions and action items, each with a checked timestamp. |
| **Documents** | DOCX, PPTX and PDF from Markdown, with Mermaid and draw.io diagrams, in each account's own templates. |
| **Agents** | An MCP server, `--json` on every command, generated `AGENTS.md` / `llms.txt`, and a Claude Code skill. |

## Safe by default

- **Local first.** Models run on your Mac through Ollama. Nothing goes to an
  external service unless the workspace policy names it.
- **Nothing is read until you approve it.** Each workspace starts with
  `processing = "pending"`; `dsr policy approve` opens it.
- **One workspace per account.** Each account's documents, templates,
  glossary and voiceprints stay in its own folder, never in this repository.
- **Logs without content.** Run reports and logs hold paths, hashes and counts.
- **Voiceprints are opt-in** and stored only for people whose consent is recorded.

## Requirements

- macOS on Apple Silicon with 16 GB of memory or more
- [Homebrew](https://brew.sh)
- About 12 GB of free disk: 8 GB for the local models, 2 GB for Python packages
- Windows: planned ([what remains](docs/windows-port.md))

## Install

From the shared bundle (`dossier-0.1.0-bundle.zip`):

```bash
unzip dossier-0.1.0-bundle.zip && cd dossier-0.1.0
brew bundle --file Brewfile          # uv, ffmpeg, pandoc, typst, mermaid-cli, Ollama...
uv tool install --python 3.12 "dossier[all] @ ./dossier-0.1.0-py3-none-any.whl"
brew services start ollama
dsr doctor                           # what is installed and what is missing
```

From a clone of the source: `uv tool install --python 3.12 "<path-to-repo>[all]"`.
Details and optional pieces: [docs/install.md](docs/install.md).

## Your first workspace

```bash
mkdir -p ~/Work/acme && cd ~/Work/acme
dsr init --name "Acme" --language es --language en   # folders + dossier.toml
dsr setup --with llm                 # pulls the local models (about 8 GB)
```

Edit `dossier.toml`: write a two-line `brief` about the account and point
`[[sources]]` at its folders (synced OneDrive or SharePoint folders work). Then:

```bash
dsr policy approve                   # once processing this material here is allowed
dsr sync                             # convert, transcribe, summarize, index
dsr search "which hosting option was chosen?"
dsr mcp install                      # connect Claude Code
```

The full walkthrough is in [docs/getting-started.md](docs/getting-started.md).

## Meetings

```bash
dsr secrets set hf-token             # once: the speaker-separation model is gated
dsr meet add "2026-11-12 Weekly.m4a" # or drop it in a source folder for dsr sync
dsr meet identify <id>               # listen to each speaker's clips, name them
dsr meet show <id>                   # summary, decisions, action items
```

List the team in `[meet] people` and turn on `speaker_review` and
`speaker_voiceprints`. After you name the speakers of the first meeting or
two, later meetings arrive with the names suggested. See
[docs/meetings.md](docs/meetings.md).

## Documents

```bash
dsr templates import "Acme report.dotx" --pack acme --kind report
dsr docs render out/status.md --to docx --template acme:report
dsr docs render out/status.md --to pdf
```

Each account keeps its template packs in its own workspace; only a neutral
pack ships with dossier. See [docs/documents.md](docs/documents.md).

## Documentation

| Guide | Covers |
|---|---|
| [Getting started](docs/getting-started.md) | Setting up a workspace for an account, end to end |
| [Install](docs/install.md) | System tools, Python extras, upgrading and removing |
| [Configuration](docs/configuration.md) | Every `dossier.toml` setting |
| [Corpus](docs/corpus.md) | What each file type becomes; sync, safety, scheduling |
| [Index and search](docs/index.md) | Cards, folder summaries, search, qmd, benchmarks |
| [Meetings](docs/meetings.md) | Languages, speakers and voiceprints, audio check, summaries, glossary |
| [Documents](docs/documents.md) | Rendering, diagrams, per-account template packs |
| [Agents](docs/agents.md) | MCP server, CLI for agents, navigation files, skill |
| [Models](docs/models.md) | Roles, small-model design, other servers, cloud |
| [Windows port](docs/windows-port.md) | What is portable, what remains |

## Commands

```text
dsr init | doctor | setup | status | version | policy | secrets
dsr sync | convert | index | schedule
dsr search | tree | read | taxonomy suggest | qmd sync
dsr meet add | list | show | review | identify | speakers | summarize | brief | eval
dsr people list | consent | forget
dsr docs render      dsr templates list | show | inspect | new | import
dsr models list | test | bench
dsr mcp serve | install      dsr skill path | install
```

Every command has `--help`, and `--json` for scripts and agents.

## Contributing

```bash
git clone dossier-0.1.0.git.bundle dossier && cd dossier   # from the bundle
uv sync --all-extras
git config core.hooksPath .githooks     # blocks personal and client data
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
uv run python tests/e2e.py              # end to end with real tools and models
```

Contributions must not contain personal data or the names of clients, projects
or people. See [CONTRIBUTING.md](CONTRIBUTING.md); changes are listed in
[CHANGELOG.md](CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md).
