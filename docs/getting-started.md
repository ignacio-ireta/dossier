# Getting started: a workspace for an account

This walks through setting up dossier for one client or account, from install to
an agent answering questions with citations.

## 1. Install (once per machine)

```bash
brew bundle --file Brewfile
uv tool install --python 3.12 "<path-or-url-to-dossier>[all]"
dsr doctor
```

`[all]` includes meetings (PyTorch, Whisper, pyannote: about 2 GB). Without
meetings: `"dossier[ocr,embed,mcp]"`. Start the model server with
`brew services start ollama` (or `ollama serve`).

## 2. Create the workspace

```bash
mkdir -p ~/Work/acme && cd ~/Work/acme
dsr init --name "Acme" --language es --language en   # the languages of the material
```

Edit `dossier.toml`:

- **`[workspace] brief`**: two or three factual sentences about the engagement
  (what it is, its main topics). Every summary uses it as context. No personal data.
- **`languages`**: e.g. `["es", "en"]`, used for OCR, and with more than one,
  each meeting's language is detected among them.
- **`[[sources]]`**: one block per folder to follow. Synced folders work:

  ```toml
  [[sources]]
  name = "sharepoint"
  path = "~/Library/CloudStorage/OneDrive-Acme/Project Falcon"
  exclude = ["**/Archive/**"]
  materialize = false        # skip files that are only in the cloud
  ```

  You can also drop files into the workspace's own `sources/` folder.

## 3. Models

```bash
dsr setup --with llm      # pulls the configured models through Ollama
dsr models test           # probes each role with a tiny request
dsr models bench          # quality on an invented corpus (optional)
```

Defaults are small local models (Gemma 4 E2B for summaries, EmbeddingGemma for
search). See [models.md](models.md) to use larger or other models.

## 4. Approve processing

Nothing is read until the workspace policy allows it:

```bash
dsr policy approve --note "engagement data; local processing permitted"
```

Content only goes to models on this machine unless you also set
`allow_external` and list a provider (see [configuration.md](configuration.md)).

## 5. First sync

```bash
dsr sync
```

1. **Convert**: every supported file in the sources becomes Markdown (and CSV
   for spreadsheets) under `corpus/`.
2. **Meetings**: recordings found in the sources are transcribed and summarized.
3. **Index**: document cards, folder summaries, `index/INDEX.md`,
   `index/TREE.md`, search, and the agent files `AGENTS.md` / `llms.txt`.

Later runs only process what changed. Keep it current automatically:

```bash
dsr schedule install --at 07:30 --watch
```

## 6. Connect an agent

```bash
dsr mcp install            # prints the `claude mcp add ...` command; --yes runs it
dsr skill install          # the dossier skill for Claude Code
```

Agents can also use the CLI: `dsr --json search "..."`, `dsr --json read <doc> --lines a-b`.

## 7. Meetings and documents

```bash
dsr meet add "2026-11-12 Steering committee.m4a" --attendee "Sam" --deck slides.pptx
dsr meet identify <id>     # listen to each speaker's clips and name them

dsr templates import "Acme report.dotx" --pack acme --kind report
dsr docs render out/status-report.md --to docx --template acme:report
```

See [meetings.md](meetings.md) and [documents.md](documents.md).

## Day to day

| I want to... | Command |
|---|---|
| See what changed and whether runs succeed | `dsr status` |
| Find something | `dsr search "question"` |
| Read a document at a place | `dsr read <doc> --lines 40-80` |
| See the workspace map | `dsr tree`, or open `index/TREE.md` |
| Re-run a degraded meeting summary | `dsr meet summarize <id>` |
| Stop all processing | `dsr policy prohibit` |
