# dossier

dossier gives AI agents the same project context a person on the team has. Point
it at a project folder and it keeps that folder's documents converted, summarized,
indexed and searchable, turns meeting recordings into transcripts with summaries
and action items, and renders documents from the account's own templates. Agents
reach all of it through an MCP server, the `dsr` CLI (every command has `--json`),
and generated navigation files.

It is local-first: everything runs on your machine with small local models such as
Gemma 4 E2B, and nothing is sent elsewhere unless a workspace's policy explicitly
allows a named external provider.

> Status: early development. Working today: workspace setup, policy, model
> routing, diagnostics, the corpus (conversion, incremental sync, scheduling),
> the index (cards, folder summaries, TREE/INDEX, hybrid search, qmd), and agent
> access (MCP server, AGENTS.md/llms.txt, Claude Code skill). Meetings and
> documents are being added milestone by milestone.

## Install (macOS)

```bash
brew bundle --file Brewfile                         # system tools: uv, ffmpeg, pandoc, ollama, ...
uv tool install --python 3.12 "<path-or-url-to-dossier>[all]"
dsr doctor                                          # what works, what is missing
```

Windows support is planned; see [docs/windows-port.md](docs/windows-port.md).

## Quick start

```bash
mkdir -p ~/Work/acme && cd ~/Work/acme
dsr init --name "Acme"            # folders + dossier.toml, processing policy "pending"
dsr setup --with llm              # pulls the configured local models
dsr models test                   # probes every model role
dsr policy approve                # once processing this material here is permitted
dsr sync                          # convert sources/ into corpus/ (and later index it)
dsr schedule install --at 07:30   # keep it current automatically
```

See [docs/corpus.md](docs/corpus.md) for what each file type becomes and
[docs/index.md](docs/index.md) for cards, folder summaries and search, and
[docs/agents.md](docs/agents.md) for the MCP server and agent files.

## Concepts

- **Workspace**: one folder per client or account, configured by `dossier.toml`.
- **Policy**: nothing is read until `processing = "approved"`. Content only goes to
  loopback model servers unless `allow_external` is set *and* the provider is listed.
- **Model roles**: tasks ask for a role (`index`, `meet`, `vision`, `embed`,
  `rerank`), and the workspace maps roles to models. Defaults are small local models.
- **Template packs**: per-account document templates, selected per workspace or per
  document.

See [docs/configuration.md](docs/configuration.md) for every setting.

## Development

```bash
uv sync --all-extras
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git config core.hooksPath .githooks   # denylist gate for personal/project data
```

Contributions must not contain personal data or names of clients, projects or
people. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md).
