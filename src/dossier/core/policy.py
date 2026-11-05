"""The workspace policy gate.

Two rules, both fail-closed:

1. Nothing reads, converts, indexes or transcribes source material until the
   workspace policy says ``processing = "approved"``. Credentials, installed
   models or a schedule are capability, not permission.
2. Content only goes to a model endpoint on this machine (loopback), unless the
   workspace explicitly allows external processing *and* names the provider.
   Scheduled (unattended) runs additionally need ``scheduled_external``.
"""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import tomlkit

from dossier.core.config import (
    PolicySection,
    ProcessingStatus,
    ProviderConfig,
    ProviderKind,
    WorkspaceConfig,
)
from dossier.core.errors import ConfigError, PolicyBlockedError
from dossier.core.io import atomic_write_text

_LOOPBACK_NAMES = {"localhost"}


def is_loopback_url(url: str) -> bool:
    """True for http(s) URLs whose host is a loopback address, without credentials."""
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises on a malformed port
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    host = parts.hostname.lower()
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_processing(config: WorkspaceConfig, action: str) -> None:
    status = config.policy.processing
    if status is not ProcessingStatus.APPROVED:
        raise PolicyBlockedError(
            f"{action} blocked: workspace policy processing={status.value}; "
            "nothing was read or processed. Run `dsr policy approve` once processing "
            "this material on this machine is permitted."
        )


def provider_is_external(provider: ProviderConfig) -> bool:
    if provider.kind is ProviderKind.ANTHROPIC:
        return True
    return not is_loopback_url(provider.base_url)


def check_provider(
    name: str, provider: ProviderConfig, policy: PolicySection, *, scheduled: bool = False
) -> None:
    """Raise unless sending workspace content to this provider is permitted."""
    if not provider_is_external(provider):
        return
    if not policy.allow_external:
        raise PolicyBlockedError(
            f"provider {name!r} is not on this machine and policy.allow_external is false"
        )
    if name not in policy.allowed_providers:
        raise PolicyBlockedError(
            f"provider {name!r} is external and not listed in policy.allowed_providers"
        )
    if scheduled and not policy.scheduled_external:
        raise PolicyBlockedError(
            f"provider {name!r} is external; unattended runs need policy.scheduled_external"
        )


def set_processing(config_path: Path, status: ProcessingStatus, *, note: str = "") -> None:
    """Edit ``[policy]`` in place, keeping the file's comments and layout."""
    try:
        document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{config_path} not found", path=config_path) from exc
    policy = document.get("policy")
    if policy is None:
        policy = tomlkit.table()
        document["policy"] = policy
    policy["processing"] = status.value
    if status is ProcessingStatus.APPROVED:
        policy["approved_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
        policy["approval_note"] = note
    else:
        policy.pop("approved_at", None)
        if note:
            policy["approval_note"] = note
    atomic_write_text(config_path, tomlkit.dumps(document))
