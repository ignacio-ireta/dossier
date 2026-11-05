from __future__ import annotations

import json
import shutil

from typer.testing import CliRunner

from dossier.cli import app
from dossier.core.workspace import Workspace
from dossier.eval.synthetic import build_corpus
from dossier.platform.fake import FakeState

runner = CliRunner()


def _approve(workspace: Workspace) -> list[str]:
    ws = ["--workspace", str(workspace.root)]
    assert runner.invoke(app, [*ws, "policy", "approve", "--yes"]).exit_code == 0
    return ws


def test_convert_json_and_report(workspace: Workspace) -> None:
    build_corpus(workspace.paths.sources)
    ws = _approve(workspace)
    result = runner.invoke(app, ["--json", *ws, "convert"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["stages"]["convert"]["main"]["counts"]["added"] == 9
    reports = list(workspace.paths.runs.glob("*/report.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["stages"][0]["name"] == "convert"
    assert report["stages"][0]["counts"]["added"] == 9
    text = reports[0].read_text()
    assert "Falcon" not in text and "1.2 million" not in text  # content-free


def test_dry_run_json(workspace: Workspace) -> None:
    build_corpus(workspace.paths.sources)
    ws = _approve(workspace)
    result = runner.invoke(app, ["--json", *ws, "sync", "--dry-run"])
    data = json.loads(result.stdout)
    assert data["dry_run"] is True and data["run_id"] is None
    assert not list(workspace.paths.runs.glob("*"))


def test_scheduled_sync_notifies_on_attention(
    workspace: Workspace, fake_platform: FakeState
) -> None:
    ws = _approve(workspace)
    shutil.rmtree(workspace.paths.sources)
    result = runner.invoke(app, [*ws, "sync", "--scheduled"])
    assert result.exit_code == 75
    assert fake_platform.notifications and "needs attention" in fake_platform.notifications[0][1]
    assert fake_platform.holds == ["dsr sync"]


def test_schedule_install_status_remove(workspace: Workspace, fake_platform: FakeState) -> None:
    ws = _approve(workspace)
    result = runner.invoke(app, ["--json", *ws, "schedule", "install", "--at", "06:45"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    job = fake_platform.jobs[data["label"]]
    assert job.daily_at == (6, 45)
    assert job.program[-4:] == ["--workspace", str(workspace.root), "sync", "--scheduled"]
    assert job.watch_paths == [workspace.paths.sources]
    status = json.loads(runner.invoke(app, ["--json", *ws, "schedule", "status"]).stdout)
    assert status["installed"] is True
    removed = json.loads(runner.invoke(app, ["--json", *ws, "schedule", "remove"]).stdout)
    assert removed["removed"] is True and not fake_platform.jobs


def test_schedule_requires_approval(workspace: Workspace) -> None:
    result = runner.invoke(app, ["--workspace", str(workspace.root), "schedule", "install"])
    assert result.exit_code != 0
