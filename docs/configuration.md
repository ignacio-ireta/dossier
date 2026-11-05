# `dossier.toml` reference

`dsr init` writes a commented file; this page explains every section.

## `[workspace]`

| Key | Meaning |
|---|---|
| `name` | Display name. |
| `brief` | One short, factual paragraph about the workspace. Models receive it as context. Keep it free of personal data. |
| `languages` | Languages of the material (e.g. `["es", "en"]`). OCR uses them as hints; with more than one, each meeting's language is detected among them. `dsr init --language es --language en` sets this. |

## `[policy]`

The policy gate fails closed.

| Key | Meaning |
|---|---|
| `processing` | `pending` (default), `approved` or `prohibited`. Sources are read only when `approved`. Change it with `dsr policy approve` / `dsr policy prohibit`. |
| `approved_at`, `approval_note` | Written by `dsr policy approve`. |
| `allow_external` | `false` by default. When false, content only goes to loopback (this-machine) model servers. |
| `allowed_providers` | Provider names that may receive content when `allow_external` is true. |
| `scheduled_external` | Whether unattended (scheduled) runs may use those external providers. |
| `data_classes` | Free-form labels for the kind of material (informational). |

## `[[sources]]`

One block per folder to keep converted and indexed.

| Key | Meaning |
|---|---|
| `name` | Short identifier (letters, digits, `.`, `_`, `-`). Converted output is grouped by it. |
| `path` | Folder path; relative paths are relative to the workspace. Synced cloud folders (OneDrive, SharePoint, iCloud) work. |
| `include`, `exclude` | Glob patterns. |
| `materialize` | `false`: skip cloud-only placeholder files. `true`: read them, which downloads them. |

## `[providers.<name>]`

| Key | Meaning |
|---|---|
| `kind` | `ollama`, `openai` (any OpenAI-compatible server: llama.cpp, LM Studio, vLLM...) or `anthropic`. |
| `base_url` | `http://127.0.0.1:11434` for Ollama; OpenAI-compatible URLs end in `/v1`. |
| `api_key_secret` | Name of a keyring secret holding the API key (`dsr secrets set <name>`). |
| `timeout_s` | Request timeout in seconds. |

## `[models.<role>]`

Roles: `index` (document cards, folder summaries), `meet` (meeting summaries and
actions), `vision` (slide and image captions), `embed` (search embeddings),
`rerank` (optional search reranking).

| Key | Meaning |
|---|---|
| `enabled` | Turn a role off without deleting it. |
| `provider`, `model` | Which server and model serve the role. |
| `context_tokens` | Context window to request (Ollama `num_ctx`). Prompts are packed to fit it. |
| `max_output_tokens` | Output cap per call. |
| `temperature` | Default `0`. |
| `think` | Reasoning mode for models that support it. Default `false` keeps small models fast. |
| `accepts_images` | Whether the model can read images (needed for `vision`). |
| `extra` | Extra request fields passed to the server as-is (e.g. a llama.cpp chat-template switch). |

### Example: an allowed cloud route

```toml
[policy]
allow_external = true
allowed_providers = ["cloud"]

[providers.cloud]
kind = "anthropic"
api_key_secret = "anthropic"     # dsr secrets set anthropic

[models.meet]
provider = "cloud"
model = "claude-opus-5-5"
max_output_tokens = 16000        # thinking counts toward the output cap
extra = { output_config = { effort = "low" } }
```

Current Claude models reject sampling parameters, so `temperature` and `think` are
ignored for this provider; tune depth with `effort` instead.

## `[[authority]]`, `[taxonomy]`

Optional. `authority` rules map path globs to a tier and label shown on document
cards. `taxonomy` lists approved topic and task keys for tagging; without it, tags
are free-form.

## `[docs]`, `[agent]`, `[meet]`

| Key | Meaning |
|---|---|
| `docs.template` | Default template pack for rendered documents. |
| `agent.allow_render` | Let MCP clients render documents. |
| `meet.language` | Spoken language of meetings: a code (`"es"`, `"en"`...) or `"auto"` to detect it per recording. Default: `"auto"` when `languages` lists several, otherwise that language. `dsr meet add --language` overrides it per meeting. |
| `meet.glossary` | Path to a glossary used for transcription (terms, mishearings, per-language sections; see [meetings](meetings.md#glossary)). |
| `meet.people` | Names of the people who usually speak: the choices offered when naming speakers. |
| `meet.speaker_review` | Prepare clips and a review page for every new meeting with unnamed speakers (`dsr meet identify`). Default `false`. |
| `meet.speaker_voiceprints` | Opt-in speaker recognition; voiceprints are biometric data and stay local. |
