from __future__ import annotations

import importlib.util
import io
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "denylist.py"


@pytest.fixture
def denylist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    terms = tmp_path / "terms.txt"
    terms.write_text("# comment\nFalcon Corp\nZoë\nre:\\bPRJ-\\d+\\b\n", encoding="utf-8")
    monkeypatch.setenv("DOSSIER_DENYLIST", str(terms))
    spec = importlib.util.spec_from_file_location("denylist_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _messages(module: ModuleType, text: str) -> list[str]:
    terms = module.load_terms(module.denylist_path())
    return [message for _label, _line, message in module.scan_text("t", text, terms)]


def test_terms_are_case_and_accent_insensitive(denylist: ModuleType) -> None:
    assert _messages(denylist, "meeting with FALCON corp today")
    assert _messages(denylist, "notes from zoe")
    assert not _messages(denylist, "falconry is unrelated; zoetrope too")


def test_regex_terms(denylist: ModuleType) -> None:
    assert _messages(denylist, "see PRJ-1234")
    assert not _messages(denylist, "see prj-1234")


# Synthetic inputs that must trigger the built-in rules.
SAMPLE_EMAIL = "write to someone@corp.io"  # denylist-ok: synthetic input
SAMPLE_MAC_HOME = "/Users/realperson/file"  # denylist-ok: synthetic input
SAMPLE_WIN_HOME = r"C:\Users\realperson\file"  # denylist-ok: synthetic input


def test_builtin_rules(denylist: ModuleType) -> None:
    assert any("email" in m for m in _messages(denylist, SAMPLE_EMAIL))
    assert not _messages(denylist, "noreply@anthropic.com alex@example.com a@b.invalid")
    assert any("home path" in m for m in _messages(denylist, SAMPLE_MAC_HOME))
    assert any("home path" in m for m in _messages(denylist, SAMPLE_WIN_HOME))
    assert not _messages(denylist, "/Users/you/file and /home/user/x")
    assert not _messages(denylist, "+@pytest.fixture and +@app.command()")  # diff lines
    assert not _messages(denylist, "Falcon Corp mention  # denylist-ok")


def test_office_files_are_scanned(denylist: ModuleType) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:t>Prepared for Falcon Corp</w:t>")
        archive.writestr("word/media/image1.png", b"\x89PNG\x00")
    terms = denylist.load_terms(denylist.denylist_path())
    findings = list(denylist.scan_blob("report.docx", buffer.getvalue(), terms))
    assert any("word/document.xml" in label for label, _line, _msg in findings)


def test_cli_exit_codes(denylist: ModuleType) -> None:
    assert denylist.main(["--text", "clean text"]) == 0
    assert denylist.main(["--text", "Falcon Corp"]) == 1


def test_history_scans_committed_office_files(
    denylist: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    env = {
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    monkeypatch.setattr("os.environ", {**__import__("os").environ, **env})
    subprocess.run(["git", "init", "-q"], check=True)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:t>Prepared for Falcon Corp</w:t>")
    (repo / "report.docx").write_bytes(buffer.getvalue())
    (repo / "noise.bin").write_bytes(bytes(range(256)) * 4 + b"\x00PRJ-77\x00")
    subprocess.run(["git", "add", "."], check=True)
    subprocess.run(
        ["git", "commit", "-qm", "add files", "--no-verify"],
        check=True,
        env={**__import__("os").environ, **env},
    )
    terms = denylist.load_terms(denylist.denylist_path())
    findings = list(denylist.run_history(terms))
    labels = [label for label, _line, _msg in findings]
    assert any("report.docx" in label and "word/document.xml" in label for label in labels)
    assert not any("noise.bin" in label for label in labels)  # binary, not text
