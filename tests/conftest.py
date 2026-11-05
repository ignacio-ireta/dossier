from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from dossier import platform
from dossier.core.workspace import Workspace, open_workspace
from dossier.ops.init import init_workspace
from dossier.platform import fake


@pytest.fixture(autouse=True)
def fake_platform(monkeypatch: pytest.MonkeyPatch) -> Iterator[fake.FakeState]:
    """Every test runs against the in-memory platform unless it opts out."""
    monkeypatch.delenv("DOSSIER_WORKSPACE", raising=False)
    state = fake.FakeState()
    platform.use(fake.build(state))
    yield state
    platform.use(None)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    init_workspace(tmp_path / "acme", name="Acme")
    return open_workspace(tmp_path / "acme")
