from __future__ import annotations

from pathlib import Path

import pytest

from dossier.core.config import (
    PolicySection,
    ProcessingStatus,
    ProviderConfig,
    ProviderKind,
    load_config,
)
from dossier.core.errors import ConfigError, PolicyBlockedError, WorkspaceNotFoundError
from dossier.core.paths import find_workspace
from dossier.core.policy import (
    check_provider,
    is_loopback_url,
    require_processing,
    set_processing,
)
from dossier.core.workspace import Workspace
from dossier.ops.init import init_workspace


def test_init_writes_valid_pending_config(workspace: Workspace) -> None:
    config = workspace.config
    assert config.workspace.name == "Acme"
    assert config.policy.processing is ProcessingStatus.PENDING
    assert config.models["index"].model == "gemma4:e2b"
    assert config.models["index"].think is False
    assert config.models["vision"].enabled is False
    for directory in workspace.paths.skeleton():
        assert directory.is_dir()


def test_init_refuses_to_overwrite(workspace: Workspace) -> None:
    with pytest.raises(ConfigError):
        init_workspace(workspace.root, name="Other")


def test_find_workspace_walks_up(workspace: Workspace) -> None:
    nested = workspace.paths.sources / "a" / "b"
    nested.mkdir(parents=True)
    assert find_workspace(nested).root == workspace.root


def test_find_workspace_env(workspace: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOSSIER_WORKSPACE", str(workspace.root))
    assert find_workspace(Path("/")).root == workspace.root
    monkeypatch.setenv("DOSSIER_WORKSPACE", str(workspace.root / "nope"))
    with pytest.raises(WorkspaceNotFoundError):
        find_workspace()


def _rewrite(workspace: Workspace, old: str, new: str) -> Path:
    path = workspace.paths.config
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")
    return path


def test_unknown_role_rejected(workspace: Workspace) -> None:
    path = _rewrite(workspace, "[models.index]", "[models.summaries]")
    with pytest.raises(ConfigError, match="unknown model roles"):
        load_config(path)


def test_route_with_unknown_provider_rejected(workspace: Workspace) -> None:
    path = _rewrite(
        workspace,
        '[models.index]           # document cards and folder summaries\nprovider = "local"',
        '[models.index]\nprovider = "nowhere"',
    )
    with pytest.raises(ConfigError, match="unknown provider"):
        load_config(path)


def test_typo_in_key_rejected(workspace: Workspace) -> None:
    path = _rewrite(workspace, "allow_external = false", "allow_externl = false")
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:11434", True),
        ("http://localhost:8080/v1", True),
        ("http://[::1]:8080/v1", True),
        ("http://127.0.0.2:8080/v1", True),
        ("https://api.example.com/v1", False),
        ("http://10.0.0.5:11434", False),
        ("http://user:pass@127.0.0.1:11434", False),
        ("ftp://127.0.0.1", False),
        ("http://127.0.0.1:notaport", False),
        ("not a url", False),
    ],
)
def test_loopback_detection(url: str, expected: bool) -> None:
    assert is_loopback_url(url) is expected


def test_processing_requires_approval(workspace: Workspace) -> None:
    with pytest.raises(PolicyBlockedError, match="processing=pending") as caught:
        require_processing(workspace.config, "sync")
    assert caught.value.exit_status == 78


def test_set_processing_keeps_comments(workspace: Workspace) -> None:
    set_processing(workspace.paths.config, ProcessingStatus.APPROVED, note="internal test data")
    text = workspace.paths.config.read_text(encoding="utf-8")
    assert "# Nothing in this workspace is read" in text
    config = load_config(workspace.paths.config)
    assert config.policy.processing is ProcessingStatus.APPROVED
    assert config.policy.approved_at
    require_processing(config, "sync")
    set_processing(workspace.paths.config, ProcessingStatus.PROHIBITED)
    assert load_config(workspace.paths.config).policy.approved_at is None


LOCAL = ProviderConfig(kind=ProviderKind.OLLAMA, base_url="http://127.0.0.1:11434")
REMOTE = ProviderConfig(kind=ProviderKind.OPENAI, base_url="https://models.example.com/v1")
CLOUD = ProviderConfig(kind=ProviderKind.ANTHROPIC)


def test_local_provider_always_allowed() -> None:
    check_provider("local", LOCAL, PolicySection(), scheduled=True)


@pytest.mark.parametrize("provider", [REMOTE, CLOUD])
def test_external_provider_rules(provider: ProviderConfig) -> None:
    with pytest.raises(PolicyBlockedError, match="allow_external"):
        check_provider("remote", provider, PolicySection())
    with pytest.raises(PolicyBlockedError, match="allowed_providers"):
        check_provider("remote", provider, PolicySection(allow_external=True))
    allowed = PolicySection(allow_external=True, allowed_providers=["remote"])
    check_provider("remote", provider, allowed)
    with pytest.raises(PolicyBlockedError, match="unattended"):
        check_provider("remote", provider, allowed, scheduled=True)
    check_provider(
        "remote",
        provider,
        PolicySection(allow_external=True, allowed_providers=["remote"], scheduled_external=True),
        scheduled=True,
    )
