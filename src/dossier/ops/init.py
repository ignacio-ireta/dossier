"""``dsr init``: create a workspace skeleton and a commented ``dossier.toml``."""

from __future__ import annotations

import json
from pathlib import Path

from dossier.core.config import load_config
from dossier.core.errors import ConfigError
from dossier.core.io import atomic_write_text
from dossier.core.languages import parse_language
from dossier.core.paths import WorkspacePaths

DEFAULT_CHAT_MODEL = "gemma4:e2b"
DEFAULT_EMBED_MODEL = "embeddinggemma"

_TEMPLATE = """\
# dossier workspace configuration. Reference: docs/configuration.md

[workspace]
name = {name}
# One short paragraph about what this workspace covers. Models get it as
# context when summarizing, so keep it factual and free of personal data.
brief = ""
# Languages of the material: OCR hints, and the languages meetings are detected
# among. With several, each recording's language is detected (see [meet]).
languages = {languages}

[policy]
# Nothing in this workspace is read or processed until this says "approved".
# Approve with `dsr policy approve` once processing this material on this
# machine is permitted.
processing = "pending"   # pending | approved | prohibited
# Content only goes to models on this machine unless both are set:
allow_external = false
allowed_providers = []   # provider names below that may receive content
scheduled_external = false

# Folders to keep converted and indexed. Paths are relative to this file
# unless absolute. Add more [[sources]] blocks for synced folders.
[[sources]]
name = "main"
path = "sources"
exclude = ["**/~$*", "**/.DS_Store", "**/Thumbs.db"]
materialize = false      # true: download cloud-only (placeholder) files

[providers.local]
kind = "ollama"          # ollama | openai (any OpenAI-compatible server) | anthropic
base_url = "http://127.0.0.1:11434"

# Model roles. Every role defaults to small local models; swap any role for a
# larger local model, or for an allowed external provider.
[models.index]           # document cards and folder summaries
provider = "local"
model = {chat_model}
context_tokens = 8192
max_output_tokens = 1024

[models.meet]            # meeting summaries and action items
provider = "local"
model = {chat_model}
context_tokens = 8192
max_output_tokens = 1024

[models.embed]           # search embeddings
provider = "local"
model = {embed_model}
# Task prompts for EmbeddingGemma; clear them for models without task prompts.
query_prefix = "task: search result | query: "
document_prefix = "title: none | text: "

[models.vision]          # slide and image captions (needs an image-capable model)
enabled = false
provider = "local"
model = {chat_model}
accepts_images = true

[index]
chunk_tokens = 350
search_backend = "native"   # native | qmd

[docs]
template = "neutral"     # default template pack for rendered documents

[agent]
allow_render = false     # let MCP clients render documents

[meet]
# language = "auto"          # or "es", "en"...; default: auto with several languages
# glossary = "glossary.toml" # terms and mishearings; see docs/meetings.md
# people = ["Alex", "Sam"]   # who usually speaks: the choices when naming speakers
speaker_review = false       # true: cut short clips of unnamed speakers for `dsr meet identify`
speaker_voiceprints = false  # opt-in; voiceprints are biometric data
"""


def render_config(
    name: str, *, chat_model: str, embed_model: str, languages: list[str] | None = None
) -> str:
    codes = [parse_language(value, allow_auto=False) for value in languages or ["en"]]
    return _TEMPLATE.format(
        name=json.dumps(name),
        languages=json.dumps(list(dict.fromkeys(codes))),
        chat_model=json.dumps(chat_model),
        embed_model=json.dumps(embed_model),
    )


def init_workspace(
    root: Path,
    *,
    name: str,
    chat_model: str = DEFAULT_CHAT_MODEL,
    embed_model: str = DEFAULT_EMBED_MODEL,
    languages: list[str] | None = None,
    force: bool = False,
) -> WorkspacePaths:
    paths = WorkspacePaths(root.resolve())
    if paths.config.exists() and not force:
        raise ConfigError(f"{paths.config} already exists; pass --force to overwrite it")
    config = render_config(
        name, chat_model=chat_model, embed_model=embed_model, languages=languages
    )  # validates the languages before anything is written
    for directory in paths.skeleton():
        directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(paths.config, config)
    load_config(paths.config)  # the template must always validate
    return paths
