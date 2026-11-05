# Contributing

## Ground rules

1. **No personal data, no client or project information.** Code, docs, examples,
   tests, fixtures, commit messages and commit metadata must not contain people's
   names, emails, account names, or the names of clients, programs or projects.
   Use generic stand-ins ("Acme", "Project Falcon", "Alex").
2. **Synthetic fixtures only.** Tests generate their inputs in code. Never commit a
   real document, recording, template or transcript, even a "scrubbed" one.
3. **OS-specific code lives in `src/dossier/platform/`.** Everything else must import
   and run on macOS and Windows; `tests/test_platform_neutrality.py` enforces it.
4. **Small models first.** Every LLM task must work with a ~2B-effective local model:
   one narrow question per call, a flat JSON schema, bounded input, and a
   deterministic fallback (see `dossier.llm.structured`).

## The denylist gate

`git config core.hooksPath .githooks` enables two hooks that run
`scripts/denylist.py`. Built-in checks catch email addresses and home-directory
paths. Add the names you must never commit (clients, colleagues, your own accounts)
to a personal list **outside** the repository:

```text
~/.config/dossier-dev/denylist.txt     (or set DOSSIER_DENYLIST)
```

One term per line; `re:` prefixes a regular expression. Before sharing a branch, run:

```bash
python3 scripts/denylist.py --all
python3 scripts/denylist.py --history
```

## Checks

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
uv run python tests/e2e.py --workdir /tmp/dossier-e2e    # real tools and models
```

Tests marked `live_model` need a running local model server; deselect them with
`-m "not live_model"`. Tests that need a system tool (ffmpeg, pandoc, typst,
mmdc, draw.io, LibreOffice) skip when it is missing. `tests/e2e.py` drives the
installed `dsr` end to end (documents, a synthetic two-voice meeting, rendering,
MCP over stdio) and needs Ollama with the default models.

## The neutral template pack

`src/dossier/docs/packs/neutral/` is generated, not hand-edited:

```bash
uv run python scripts/build_neutral_pack.py
```
