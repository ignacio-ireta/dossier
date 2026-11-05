from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

from dossier import platform
from dossier.core.errors import CapabilityUnavailableError
from dossier.platform import generic, windows
from dossier.platform.base import ScheduledJob


def test_current_returns_installed_fake() -> None:
    assert platform.current().name == "fake"


def test_windows_bundle_is_conservative() -> None:
    bundle = windows.build()
    assert bundle.name == "windows"
    assert bundle.process.pid_alive(999_999) is True  # never steal a lock
    assert not bundle.ocr.available()
    assert "Windows" in bundle.ocr.detail()
    with pytest.raises(CapabilityUnavailableError):
        bundle.scheduler.install(ScheduledJob("x", ["dsr"], Path(), Path("log")))
    assert bundle.placeholders.is_placeholder(Path("anything")) is False
    with bundle.keep_awake.hold("test"):
        pass


def test_generic_bundle() -> None:
    bundle = generic.build()
    assert {c.name for c in bundle.capabilities()} == {
        "process",
        "notify",
        "keep_awake",
        "cloud_placeholders",
        "ocr",
        "tts",
        "scheduler",
        "player",
    }
    with pytest.raises(CapabilityUnavailableError):
        bundle.tts.synthesize("hi", Path("x.wav"))


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS backend")
def test_launchd_plist_render(tmp_path: Path) -> None:
    from dossier.platform.macos import DatalessPlaceholders, LaunchdScheduler

    scheduler = LaunchdScheduler(agents_dir=tmp_path)
    job = ScheduledJob(
        label="com.example.dossier.acme",
        program=["/usr/local/bin/dsr", "sync", "--scheduled"],
        working_dir=tmp_path,
        log_path=tmp_path / "log.txt",
        daily_at=(7, 30),
        watch_paths=[tmp_path / "sources"],
        environment={"PATH": "/usr/bin"},
    )
    plist = plistlib.loads(scheduler.render(job))
    assert plist["Label"] == job.label
    assert plist["StartCalendarInterval"] == {"Hour": 7, "Minute": 30}
    assert plist["WatchPaths"] == [str(tmp_path / "sources")]
    assert plist["ProgramArguments"][-1] == "--scheduled"
    regular = tmp_path / "file.txt"
    regular.write_text("x", encoding="utf-8")
    assert DatalessPlaceholders().is_placeholder(regular) is False


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS backend")
def test_macos_detection_without_override() -> None:
    platform.use(None)
    assert platform.current().name == "macos"
