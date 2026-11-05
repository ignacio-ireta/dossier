"""``dsr setup``: install what selected features need.

Setup only ever runs commands it has printed first, and only with ``--yes`` or
an interactive confirmation. It installs system tools through the platform's
package manager (Homebrew on macOS) and pulls the models a workspace names.
dossier's own Python extras are installed by ``uv tool install``; setup prints
that command rather than modifying its own running environment.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

from dossier import platform
from dossier.core.config import ProviderKind, WorkspaceConfig
from dossier.ops.tools import FEATURES, tools_for

# Python extras whose presence is checked by module name. OCR is checked through
# the platform seam instead, because its backend differs per OS.
_FEATURE_EXTRAS = {"llm": ("embed", "sqlite_vec")}


@dataclass
class SetupPlan:
    commands: list[list[str]] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)
    satisfied: list[str] = field(default_factory=list)


def plan_setup(features: set[str], config: WorkspaceConfig | None) -> SetupPlan:
    unknown = features - set(FEATURES)
    if unknown:
        raise ValueError(f"unknown features {sorted(unknown)}; choose from {sorted(FEATURES)}")
    plan = SetupPlan()
    planned: set[str] = set()
    for tool in tools_for(features):
        location = tool.locate()
        if location:
            plan.satisfied.append(f"{tool.key}: {location}")
            planned.add(tool.key)
            continue
        hint = tool.install_hint()
        if sys.platform == "darwin" and shutil.which("brew") and hint.startswith("brew "):
            plan.commands.append(hint.split())
            planned.add(tool.key)
        elif hint.startswith("bun install") and "bun" in planned:
            plan.commands.append(hint.split())  # bun is present or installed just before
        else:
            plan.manual.append(f"{tool.key}: {hint}")
    extras = sorted(
        extra
        for feature, (extra, module) in _FEATURE_EXTRAS.items()
        if feature in features and importlib.util.find_spec(module) is None
    )
    if "ocr" in features and not platform.current().ocr.available():
        extras.append("ocr")
    if extras:
        plan.manual.append(
            "python extras: uv tool install --reinstall --python 3.12 "
            f"'dossier[{','.join(extras)}]'"
        )
    if "llm" in features and config is not None:
        for model in _ollama_models(config):
            plan.commands.append(["ollama", "pull", model])
    return plan


def _ollama_models(config: WorkspaceConfig) -> list[str]:
    models: list[str] = []
    for route in config.models.values():
        if not route.enabled:
            continue
        provider = config.providers.get(route.provider)
        if provider and provider.kind is ProviderKind.OLLAMA and route.model not in models:
            models.append(route.model)
    return models


def run_commands(commands: list[list[str]]) -> list[tuple[list[str], int]]:
    results: list[tuple[list[str], int]] = []
    for command in commands:
        completed = subprocess.run(command, check=False)
        results.append((command, completed.returncode))
        if completed.returncode != 0:
            break
    return results
