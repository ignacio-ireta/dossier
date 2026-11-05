"""Workspace configuration (``dossier.toml``), validated with pydantic.

The file is the single place an account team describes its workspace: what it
is about, where its sources live, what may be processed and by which models,
and which document templates to use.
"""

from __future__ import annotations

import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from dossier.core.errors import ConfigError
from dossier.core.languages import language_code

ROLE_NAMES = ("index", "meet", "vision", "embed", "rerank")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProcessingStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    PROHIBITED = "prohibited"


class WorkspaceSection(_Strict):
    name: str = Field(min_length=1, max_length=120)
    brief: str = Field(
        default="",
        max_length=2000,
        description="Short description of the workspace, given to models as context.",
    )
    languages: list[str] = Field(default_factory=lambda: ["en"])


class PolicySection(_Strict):
    processing: ProcessingStatus = ProcessingStatus.PENDING
    approved_at: str | None = None
    approval_note: str = ""
    allow_external: bool = False
    allowed_providers: list[str] = Field(default_factory=list)
    scheduled_external: bool = False
    data_classes: list[str] = Field(default_factory=list)


class SourceRoot(_Strict):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    path: str
    include: list[str] = Field(default_factory=lambda: ["**/*"])
    exclude: list[str] = Field(default_factory=list)
    materialize: bool = False


class ProviderKind(StrEnum):
    OLLAMA = "ollama"
    OPENAI = "openai"  # any OpenAI-compatible server: llama.cpp, LM Studio, vLLM...
    ANTHROPIC = "anthropic"


class ProviderConfig(_Strict):
    kind: ProviderKind
    base_url: str = ""
    api_key_secret: str | None = Field(
        default=None, description="Name of a secret in the OS keyring holding the API key."
    )
    timeout_s: float = Field(default=600, gt=0)

    @model_validator(mode="after")
    def _url_required(self) -> ProviderConfig:
        if self.kind is not ProviderKind.ANTHROPIC and not self.base_url:
            raise ValueError(f"{self.kind} providers need a base_url")
        return self


class ModelRoute(_Strict):
    enabled: bool = True
    provider: str = "local"
    model: str = ""
    context_tokens: int = Field(default=8192, ge=512, le=1_000_000)
    max_output_tokens: int = Field(default=1024, ge=16, le=65_536)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    accepts_images: bool = False
    think: bool | None = Field(
        default=False,
        description="Reasoning mode for models that support it; false keeps small models fast.",
    )
    extra: dict[str, Any] = Field(
        default_factory=dict, description="Extra request fields passed through to the server."
    )
    # Embedding models trained with task prompts (e.g. EmbeddingGemma) expect them.
    query_prefix: str = ""
    document_prefix: str = ""

    @model_validator(mode="after")
    def _model_required(self) -> ModelRoute:
        if self.enabled and not self.model:
            raise ValueError("an enabled model route needs a model name")
        return self

    @property
    def input_budget_tokens(self) -> int:
        """Tokens left for prompt input after reserving the output and a safety margin."""
        return max(256, self.context_tokens - self.max_output_tokens - 256)


class AuthorityRule(_Strict):
    glob: str
    tier: int = Field(ge=1, le=9)
    label: str = ""


class TaxonomyEntry(_Strict):
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    label: str = ""
    description: str = ""


class TaxonomySection(_Strict):
    topics: list[TaxonomyEntry] = Field(default_factory=list)
    tasks: list[TaxonomyEntry] = Field(default_factory=list)


class OcrMode(StrEnum):
    OFF = "off"
    AUTO = "auto"  # OCR images and pages without a text layer
    FORCE = "force"  # OCR every PDF page, ignoring embedded text


class ConvertSection(_Strict):
    ocr: OcrMode = OcrMode.AUTO
    max_file_mb: int = Field(default=200, ge=1, le=10_000)
    max_sheet_rows: int = Field(default=50_000, ge=10)
    preview_rows: int = Field(default=20, ge=1, le=500)
    mass_delete_ratio: float = Field(
        default=0.5,
        gt=0,
        le=1,
        description="Refuse a sync that would delete more than this share of a source.",
    )


class SearchBackend(StrEnum):
    NATIVE = "native"
    QMD = "qmd"


class IndexSection(_Strict):
    chunk_tokens: int = Field(default=350, ge=64, le=4000)
    redo_degraded: bool = Field(
        default=True, description="Regenerate fallback cards once the model is reachable."
    )
    max_windows_per_document: int = Field(default=24, ge=1, le=500)
    search_backend: SearchBackend = SearchBackend.NATIVE
    qmd_index: str | None = Field(
        default=None, description="qmd index name (default: derived from the workspace)."
    )


class DocsSection(_Strict):
    template: str = "neutral"


class AgentSection(_Strict):
    allow_render: bool = False


class AsrBackendName(StrEnum):
    AUTO = "auto"  # MLX Whisper on Apple Silicon, otherwise faster-whisper
    MLX = "mlx"
    FASTER_WHISPER = "faster-whisper"


class MeetSection(_Strict):
    glossary: str | None = Field(
        default=None, description="Path to glossary.toml (terms and replacements)."
    )
    language: str | None = Field(
        default=None,
        description=(
            'Spoken language: a code such as "es" or "en", or "auto" to detect it per '
            "recording. Default: auto when [workspace] languages lists several, else that one."
        ),
    )
    asr_backend: AsrBackendName = AsrBackendName.AUTO
    asr_model: str | None = None
    diarization_model: str = "pyannote/speaker-diarization-community-1"
    auto_transcribe: bool = Field(
        default=True, description="Transcribe recordings found in source folders during sync."
    )
    keep_audio: bool = False
    speaker_voiceprints: bool = Field(
        default=False, description="Opt-in speaker recognition; voiceprints are biometric data."
    )
    voiceprint_threshold: float = Field(default=0.6, ge=0.0, le=1.0)

    @field_validator("language")
    @classmethod
    def _known_language(cls, value: str | None) -> str | None:
        if value is None:
            return None
        code = language_code(value)
        if code is None:
            raise ValueError(f'unknown language {value!r}; use a code such as "es", or "auto"')
        return code


class WorkspaceConfig(_Strict):
    workspace: WorkspaceSection
    policy: PolicySection = Field(default_factory=PolicySection)
    sources: list[SourceRoot] = Field(default_factory=list)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    models: dict[str, ModelRoute] = Field(default_factory=dict)
    authority: list[AuthorityRule] = Field(default_factory=list)
    taxonomy: TaxonomySection = Field(default_factory=TaxonomySection)
    convert: ConvertSection = Field(default_factory=ConvertSection)
    index: IndexSection = Field(default_factory=IndexSection)
    docs: DocsSection = Field(default_factory=DocsSection)
    agent: AgentSection = Field(default_factory=AgentSection)
    meet: MeetSection = Field(default_factory=MeetSection)
    schema_version: Literal[1] = 1

    @field_validator("models")
    @classmethod
    def _known_roles(cls, value: dict[str, ModelRoute]) -> dict[str, ModelRoute]:
        unknown = sorted(set(value) - set(ROLE_NAMES))
        if unknown:
            raise ValueError(f"unknown model roles {unknown}; expected some of {ROLE_NAMES}")
        return value

    @model_validator(mode="after")
    def _references_resolve(self) -> WorkspaceConfig:
        for role, route in self.models.items():
            if route.enabled and route.provider not in self.providers:
                raise ValueError(f"models.{role} uses unknown provider {route.provider!r}")
        unknown_allowed = sorted(set(self.policy.allowed_providers) - set(self.providers))
        if unknown_allowed:
            raise ValueError(f"policy.allowed_providers names unknown providers {unknown_allowed}")
        names = [source.name for source in self.sources]
        if len(names) != len(set(names)):
            raise ValueError("source names must be unique")
        return self


def load_config(path: Path) -> WorkspaceConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{path} not found", path=path) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}", path=path) from exc
    try:
        return WorkspaceConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise ConfigError(f"{path} is invalid: {problems}", path=path) from exc
