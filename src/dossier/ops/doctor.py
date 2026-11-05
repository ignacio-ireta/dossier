"""``dsr doctor``: what works on this machine, what is missing, how to fix it.

Doctor never reads workspace content. It inspects the environment, the
configuration, and (for loopback providers only) which models a server offers.
"""

from __future__ import annotations

import importlib.util
import platform as stdlib_platform
import sys
from dataclasses import asdict, dataclass
from enum import StrEnum

from dossier import __version__, platform
from dossier.core.config import ProcessingStatus, ProviderConfig
from dossier.core.errors import DossierError
from dossier.core.policy import provider_is_external
from dossier.core.workspace import Workspace
from dossier.llm.providers import build_provider
from dossier.ops.tools import TOOLS


class Level(StrEnum):
    OK = "ok"
    INFO = "info"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    group: str
    name: str
    level: Level
    detail: str
    fix: str = ""

    def as_dict(self) -> dict[str, str]:
        data = asdict(self)
        data["level"] = self.level.value
        return data


_EXTRAS = (
    ("embed", "sqlite_vec", "vector search"),
    ("mcp", "mcp", "MCP server"),
    ("meet", "pyannote", "meetings: transcription and diarization"),
    ("cloud", "anthropic", "Anthropic provider"),
)


def runtime_checks() -> list[Check]:
    checks = [
        Check("runtime", "dossier", Level.OK, __version__),
        Check(
            "runtime",
            "python",
            Level.OK if sys.version_info[:2] == (3, 12) else Level.WARN,
            stdlib_platform.python_version(),
            "" if sys.version_info[:2] == (3, 12) else "dossier is tested on Python 3.12",
        ),
    ]
    current = platform.current()
    checks.append(Check("runtime", "platform", Level.OK, current.name))
    for capability in current.capabilities():
        checks.append(
            Check(
                "platform",
                capability.name,
                Level.OK if capability.available() else Level.INFO,
                capability.detail(),
            )
        )
    return checks


def tool_checks() -> list[Check]:
    checks: list[Check] = []
    for tool in TOOLS:
        path = tool.locate()
        used_by = ", ".join(tool.features)
        if path:
            checks.append(Check("tools", tool.key, Level.OK, f"{path} ({used_by})"))
        else:
            checks.append(
                Check(
                    "tools",
                    tool.key,
                    Level.INFO if tool.optional else Level.WARN,
                    f"not found; needed for {used_by}: {tool.purpose}",
                    tool.install_hint(),
                )
            )
    return checks


def extra_checks() -> list[Check]:
    checks: list[Check] = []
    for extra, module, purpose in _EXTRAS:
        present = importlib.util.find_spec(module) is not None
        checks.append(
            Check(
                "extras",
                extra,
                Level.OK if present else Level.INFO,
                purpose if present else f"not installed ({purpose})",
                "" if present else f"uv tool install --reinstall 'dossier[{extra}]'",
            )
        )
    return checks


def _probe_models(name: str, config: ProviderConfig) -> tuple[list[str] | None, str]:
    quick = config.model_copy(update={"timeout_s": 5.0})
    try:
        provider = build_provider(name, quick)
        return provider.list_models(), ""
    except DossierError as exc:
        return None, exc.message


def workspace_checks(workspace: Workspace) -> list[Check]:
    config = workspace.config
    checks = [Check("workspace", "root", Level.OK, str(workspace.root))]
    status = config.policy.processing
    if status is ProcessingStatus.APPROVED:
        checks.append(Check("workspace", "policy", Level.OK, "processing approved"))
    else:
        checks.append(
            Check(
                "workspace",
                "policy",
                Level.WARN,
                f"processing={status.value}: sources will not be processed",
                "dsr policy approve" if status is ProcessingStatus.PENDING else "",
            )
        )
    for source in config.sources:
        path = workspace.paths.resolve(source.path)
        checks.append(
            Check(
                "sources",
                source.name,
                Level.OK if path.is_dir() else Level.WARN,
                str(path) if path.is_dir() else f"{path} does not exist",
            )
        )
    offered: dict[str, list[str] | None] = {}
    for name, provider_config in config.providers.items():
        if provider_is_external(provider_config):
            allowed = config.policy.allow_external and name in config.policy.allowed_providers
            checks.append(
                Check(
                    "providers",
                    name,
                    Level.INFO,
                    f"external ({provider_config.kind.value}); "
                    + ("allowed by policy" if allowed else "blocked by policy"),
                )
            )
            continue
        models, error = _probe_models(name, provider_config)
        offered[name] = models
        if models is None:
            checks.append(
                Check(
                    "providers",
                    name,
                    Level.WARN,
                    f"{provider_config.base_url} unreachable: {error}",
                    "ollama serve" if provider_config.kind.value == "ollama" else "",
                )
            )
        else:
            checks.append(
                Check(
                    "providers", name, Level.OK, f"{provider_config.base_url}, {len(models)} models"
                )
            )
    for role, route in config.models.items():
        if not route.enabled:
            checks.append(Check("models", role, Level.INFO, "disabled"))
            continue
        available = offered.get(route.provider)
        label = f"{route.model} via {route.provider}"
        if available is None:
            checks.append(Check("models", role, Level.INFO, f"{label} (not checked)"))
        elif _model_present(route.model, available):
            checks.append(Check("models", role, Level.OK, label))
        else:
            checks.append(
                Check(
                    "models",
                    role,
                    Level.WARN,
                    f"{label}: model not found on server",
                    f"ollama pull {route.model}"
                    if config.providers[route.provider].kind.value == "ollama"
                    else "",
                )
            )
    return checks


def _model_present(model: str, available: list[str]) -> bool:
    if model in available:
        return True
    # Ollama reports "name:latest" for an untagged pull.
    return ":" not in model and f"{model}:latest" in available


def run_doctor(workspace: Workspace | None) -> list[Check]:
    checks = runtime_checks() + tool_checks() + extra_checks()
    if workspace is not None:
        checks += workspace_checks(workspace)
    else:
        checks.append(Check("workspace", "root", Level.INFO, "no workspace here (dsr init)"))
    return checks
