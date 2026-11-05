"""Per-role model routing.

Each task asks for a *role* (``index``, ``meet``, ``vision``, ``embed``,
``rerank``), never for a model. The workspace maps roles to provider + model,
so one team can run everything on Gemma 4 E2B while another points ``meet`` at
a larger local model, without code changes. The policy gate is checked here,
once, before any content can reach a provider.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from dossier.core.config import ModelRoute, ProviderConfig, WorkspaceConfig
from dossier.core.errors import ConfigError
from dossier.core.policy import check_provider
from dossier.llm.providers import build_provider
from dossier.llm.types import ChatResponse, JsonSchema, Message, Provider, RerankResult

ProviderFactory = Callable[[str, ProviderConfig], Provider]


@dataclass
class RoleClient:
    role: str
    route: ModelRoute
    provider: Provider
    provider_name: str

    @property
    def model(self) -> str:
        return self.route.model

    @property
    def input_budget_tokens(self) -> int:
        return self.route.input_budget_tokens

    def chat(
        self, messages: list[Message], *, json_schema: JsonSchema | None = None
    ) -> ChatResponse:
        return self.provider.chat(
            self.route.model,
            messages,
            json_schema=json_schema,
            max_tokens=self.route.max_output_tokens,
            temperature=self.route.temperature,
            context_tokens=self.route.context_tokens,
            think=self.route.think,
            extra=self.route.extra or None,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.provider.embed(self.route.model, texts)

    def rerank(self, query: str, documents: list[str]) -> list[RerankResult]:
        return self.provider.rerank(self.route.model, query, documents)


class ModelRouter:
    def __init__(
        self,
        config: WorkspaceConfig,
        *,
        scheduled: bool = False,
        provider_factory: ProviderFactory | None = None,
    ) -> None:
        self.config = config
        self.scheduled = scheduled
        self._factory: ProviderFactory = provider_factory or build_provider
        self._providers: dict[str, Provider] = {}

    def has(self, role: str) -> bool:
        route = self.config.models.get(role)
        return route is not None and route.enabled

    def route(self, role: str) -> ModelRoute:
        route = self.config.models.get(role)
        if route is None or not route.enabled:
            raise ConfigError(f"no enabled model for role {role!r}; add [models.{role}]")
        return route

    def client(self, role: str) -> RoleClient:
        route = self.route(role)
        provider_config = self.config.providers[route.provider]
        check_provider(
            route.provider, provider_config, self.config.policy, scheduled=self.scheduled
        )
        provider = self._providers.get(route.provider)
        if provider is None:
            provider = self._factory(route.provider, provider_config)
            self._providers[route.provider] = provider
        return RoleClient(role=role, route=route, provider=provider, provider_name=route.provider)
