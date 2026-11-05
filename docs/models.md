# Choosing models

dossier never asks for a model by name in code: every task uses a **role**, and
`dossier.toml` maps roles to a provider and model.

| Role | Used for | Default |
|---|---|---|
| `index` | document cards, folder summaries, taxonomy suggestions | `gemma4:e2b` (Ollama) |
| `meet` | meeting summaries, decisions, actions, questions | `gemma4:e2b` (Ollama) |
| `embed` | search embeddings | `embeddinggemma` (Ollama) |
| `vision` | slide captions (off by default) | an image-capable model |
| `rerank` | optional search reranking | off |

## Why small models work here

Every call asks one narrow question (summarize this window, list actions stated
here) with bounded input, gets a tiny JSON object back, is validated, repaired
once if needed, and falls back to a non-model result if it still fails. Meeting
items must cite a time that exists in the transcript. On the invented benchmark
corpus (`dsr models bench`), Gemma 4 E2B with EmbeddingGemma put every planted
fact in its document card and returned every planted question's document at
rank 1, indexing nine documents in about 7-10 seconds on an Apple Silicon laptop.

## Constrained output

When the server supports it, dossier also enforces the JSON shape with
constrained decoding (Ollama `format`, OpenAI `response_format`). Some builds
cannot: Homebrew's Ollama 0.40 runs Gemma 4 on its MLX engine without the
grammar library and answers HTTP 501. dossier detects that once per model, then
relies on the prompted shape plus validation; `dsr models test` reports which
mode a model uses.

## Using other models

Any model on Ollama, LM Studio, llama.cpp (`llama-server`) or another
OpenAI-compatible server on this machine:

```toml
[providers.lmstudio]
kind = "openai"
base_url = "http://127.0.0.1:1234/v1"

[models.meet]
provider = "lmstudio"
model = "qwen3-14b"
context_tokens = 32768
max_output_tokens = 2048
```

- Raise `context_tokens` for larger models: fewer windows, fewer calls.
- `think = false` (the default) keeps reasoning models fast; set `true` to allow it.
- `extra` passes server-specific request fields, e.g. llama.cpp's
  `chat_template_kwargs = { enable_thinking = false }`.
- Changing the `index` model regenerates cards on the next sync.
- Changing the `embed` model rebuilds the vectors on the next `dsr index`.

Run `dsr models bench` after a change to compare.

## Embedding prompts

EmbeddingGemma expects task prefixes; the template sets them:

```toml
[models.embed]
query_prefix = "task: search result | query: "
document_prefix = "title: none | text: "
```

Clear both for embedding models without task prompts.

## Reranking

A llama.cpp server started with a reranker model exposes `/v1/rerank`:

```toml
[providers.rerank]
kind = "openai"
base_url = "http://127.0.0.1:8081/v1"

[models.rerank]
provider = "rerank"
model = "reranker"
```

## Cloud models

Allowed only when the policy says so, per provider (see
[configuration.md](configuration.md)). Unattended scheduled runs need
`scheduled_external = true` as well.

## Speech models

Meetings use Whisper (MLX on Apple Silicon, faster-whisper elsewhere; set
`[meet] asr_backend` and `asr_model`) and pyannote for speakers. They download
on first use into the Hugging Face cache.
