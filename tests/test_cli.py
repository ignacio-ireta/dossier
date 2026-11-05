from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dossier.cli import app, main
from dossier.core.workspace import Workspace

runner = CliRunner()


def test_init_and_status_json(tmp_path: Path) -> None:
    target = tmp_path / "falcon"
    result = runner.invoke(app, ["--json", "init", str(target), "--name", "Project Falcon"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["workspace"] == str(target.resolve())

    result = runner.invoke(app, ["--json", "--workspace", str(target), "status"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["workspace"] == "Project Falcon"
    assert data["policy"] == "pending"
    assert data["latest_run"] is None


def test_init_languages(tmp_path: Path) -> None:
    from dossier.core.workspace import open_workspace
    from dossier.meet.pipeline import default_language

    target = tmp_path / "bilingual"
    args = ["init", str(target), "--language", "es-MX", "--language", "English"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    config = open_workspace(target).config
    assert config.workspace.languages == ["es", "en"]
    assert default_language(config) == "auto"

    result = runner.invoke(app, ["init", str(tmp_path / "bad"), "--language", "elvish"])
    assert result.exit_code != 0 and "unknown language" in str(result.exception)
    assert not (tmp_path / "bad").exists()  # nothing written


def test_meet_add_rejects_unknown_language(workspace: Workspace, tmp_path: Path) -> None:
    recording = tmp_path / "call.wav"
    recording.write_bytes(b"")
    args = ["--workspace", str(workspace.root), "meet", "add", str(recording)]
    result = runner.invoke(app, [*args, "--language", "xx"])
    assert result.exit_code == 2 and "unknown language" in result.output


def test_policy_approve_and_show(workspace: Workspace) -> None:
    ws = ["--workspace", str(workspace.root)]
    result = runner.invoke(app, [*ws, "policy", "approve", "--yes", "--note", "synthetic data"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["--json", *ws, "policy", "show"])
    data = json.loads(result.stdout)
    assert data["processing"] == "approved"
    assert data["approval_note"] == "synthetic data"


def test_models_list_json(workspace: Workspace) -> None:
    result = runner.invoke(app, ["--json", "--workspace", str(workspace.root), "models", "list"])
    rows = {row["role"]: row for row in json.loads(result.stdout)}
    assert rows["index"]["model"] == "gemma4:e2b"
    assert rows["embed"]["model"] == "embeddinggemma"
    assert rows["vision"]["enabled"] is False
    assert rows["rerank"]["configured"] is False


def test_doctor_json_without_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--json", "doctor"])
    assert result.exit_code == 0, result.output
    checks = json.loads(result.stdout)
    names = {(c["group"], c["name"]) for c in checks}
    assert ("runtime", "python") in names
    assert ("platform", "ocr") in names
    assert ("workspace", "root") in names


def test_doctor_reports_unreachable_provider(workspace: Workspace) -> None:
    path = workspace.paths.config
    path.write_text(
        path.read_text(encoding="utf-8").replace("127.0.0.1:11434", "127.0.0.1:9"),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["--json", "--workspace", str(workspace.root), "doctor"])
    checks = {(c["group"], c["name"]): c for c in json.loads(result.stdout)}
    assert checks[("providers", "local")]["level"] == "warn"
    assert checks[("workspace", "policy")]["level"] == "warn"


def test_main_maps_errors_to_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["dsr", "--json", "status"])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2
    assert json.loads(capfd.readouterr().out)["error"]["code"] == "workspace_not_found"


def test_sync_without_approval_exits_78(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["dsr", "--json", "--workspace", str(workspace.root), "sync"])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 78
    assert json.loads(capfd.readouterr().out)["error"]["code"] == "policy_blocked"
    assert not any(workspace.paths.runs.iterdir())  # nothing ran, nothing recorded
