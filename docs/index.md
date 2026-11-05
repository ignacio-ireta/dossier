# Index, summaries and search

The `index` stage of `dsr sync` (or `dsr index` on its own) turns the converted
corpus into material an agent can navigate without reading everything.

## What it writes

| Path | Contents |
|---|---|
| `index/INDEX.md` | Entry point: how to navigate, freshness, sources, documents by type and by topic, what changed last run |
| `index/TREE.md` | The whole workspace: one line per folder and per document |
| `index/folders/<path>/_folder.md` | What a folder holds, its subfolders, and a table of its documents |
| `index/cards/<doc>.card.md` | A document's summary, key points, topics, and an outline with line numbers |
| `index/index.json` | The same, machine-readable |
| `.dossier/search.db` | Chunks, keyword index (FTS5) and vectors (sqlite-vec) |

Agents should read `INDEX.md` → `TREE.md` → folder pages → cards, and only then
open a document (`dsr read <doc> --lines a-b`) or search.

## How cards are made with a small model

- A document that fits the `index` role's input budget gets **one** call.
- A longer one is split along its own headings into windows; each window gets
  a short summary, then one more call writes the card from those summaries.
- Each call returns a tiny JSON object. Invalid output is repaired once; if that
  still fails, the card is built without a model (first sentences, headings,
  frequent terms) and marked `degraded`.
- If the model server is down, every card is `degraded`, the run exits 75
  ("needs attention"), and the next run with the model back regenerates them.
- Folder summaries are built only from their children's one-line summaries, so
  any depth fits a small context. A folder is re-summarized only when something
  inside it changed.

Only documents whose converted output changed are re-indexed. Changing the
`index` model, the card format or the chunker regenerates cards on the next run.

## Search

```bash
dsr search "which hosting option was chosen?"            # human-readable
dsr --json search "budget approval" -k 5 --type docx      # for agents
```

Keyword (BM25, accent-insensitive) and semantic (embedding) results are fused
with reciprocal-rank fusion; an optional `rerank` role (e.g. a llama.cpp server
with a reranker model) reorders the top results. Each hit has the document, the
heading path, the line range, a snippet, and links to the converted text and card.
Without an embedding model (or the `embed` extra), search is keyword-only.

Filters: `--source`, `--path <prefix>`, `--type pdf`, `--kind meeting|document`.

## Topics

Cards carry free-form topics. Once a workspace has enough documents,
`dsr taxonomy suggest` proposes a controlled list (written to
`index/taxonomy-suggestion.toml` for review). Paste it into `dossier.toml` under
`[taxonomy]`; from then on cards only use those topics.

## qmd

```bash
dsr setup --with qmd       # brew install bun, then bun install -g @tobilu/qmd
dsr qmd sync               # collections + folder summaries as qmd contexts, then qmd update/embed
dsr search "..." --backend qmd
```

dossier keeps its own qmd index per workspace (`qmd --index <name>`), so personal
qmd collections are not touched. Set `[index] search_backend = "qmd"` to make it
the default. qmd downloads its own models into its cache.

## Measuring models

```bash
dsr models bench
```

Builds a throwaway workspace from an invented corpus with planted facts, using
this workspace's model roles, and reports cards made by the model vs. fallbacks,
fact recall in cards, retrieval hit@5, time and model calls. With Gemma 4 E2B
and EmbeddingGemma on Ollama, every planted fact was in its card and every
planted question found its document at rank 1.
