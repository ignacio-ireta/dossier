"""Portable fallbacks: null objects and capabilities that need no OS integration.

Used directly on unsupported systems and as building blocks for the OS modules.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from dossier.core.errors import CapabilityUnavailableError
from dossier.platform.base import (
    OcrLine,
    Platform,
    ScheduledJob,
    ScheduleStatus,
)

log = logging.getLogger("dossier.platform")


class PosixProcessProbe:
    name = "process"

    def available(self) -> bool:
        return True

    def detail(self) -> str:
        return "signal-0 probe"

    def pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, owned by another user
        except OSError:
            return True  # cannot tell: treat as alive so no live lock is stolen
        return True


class ConservativeProcessProbe:
    """Answers "alive" for every pid: never steals a lock it cannot verify."""

    name = "process"

    def available(self) -> bool:
        return True

    def detail(self) -> str:
        return "conservative (stale locks must be cleared by hand)"

    def pid_alive(self, pid: int) -> bool:
        del pid
        return True


class LogNotifier:
    name = "notify"

    def available(self) -> bool:
        return False

    def detail(self) -> str:
        return "no desktop notifications; messages go to the log"

    def notify(self, title: str, message: str) -> None:
        log.warning("notification: %s: %s", title, message)


class NoKeepAwake:
    name = "keep_awake"

    def available(self) -> bool:
        return False

    def detail(self) -> str:
        return "not supported; long runs may be interrupted by sleep"

    @contextmanager
    def hold(self, reason: str) -> Iterator[None]:
        del reason
        yield


class NoPlaceholders:
    name = "cloud_placeholders"

    def available(self) -> bool:
        return False

    def detail(self) -> str:
        return "no cloud-placeholder detection; every file is treated as local"

    def is_placeholder(self, path: Path) -> bool:
        del path
        return False


class UnavailableOcr:
    name = "ocr"

    def __init__(self, reason: str = "no OCR backend on this platform") -> None:
        self._reason = reason

    def available(self) -> bool:
        return False

    def detail(self) -> str:
        return self._reason

    def recognize(self, image: Path, *, languages: list[str] | None = None) -> list[OcrLine]:
        del image, languages
        raise CapabilityUnavailableError(f"OCR unavailable: {self._reason}")


class UnavailableTts:
    name = "tts"

    def __init__(self, reason: str = "no speech synthesis backend on this platform") -> None:
        self._reason = reason

    def available(self) -> bool:
        return False

    def detail(self) -> str:
        return self._reason

    def voices(self) -> list[str]:
        return []

    def synthesize(self, text: str, output: Path, *, voice: str | None = None) -> None:
        del text, output, voice
        raise CapabilityUnavailableError(f"speech synthesis unavailable: {self._reason}")


class FfplayPlayer:
    """Plays audio with ffplay (part of ffmpeg on Windows and Linux builds)."""

    name = "player"

    def available(self) -> bool:
        return shutil.which("ffplay") is not None

    def detail(self) -> str:
        return "ffplay" if self.available() else "ffplay not found (it comes with ffmpeg)"

    def play(self, path: Path) -> None:
        program = shutil.which("ffplay")
        if program is None:
            raise CapabilityUnavailableError("audio playback needs ffplay (part of ffmpeg)")
        command = [program, "-nodisp", "-autoexit", "-loglevel", "error", str(path)]
        subprocess.run(command, check=False, capture_output=True, timeout=600)


class UnavailableScheduler:
    name = "scheduler"

    def __init__(self, reason: str = "no scheduler backend on this platform") -> None:
        self._reason = reason

    def available(self) -> bool:
        return False

    def detail(self) -> str:
        return self._reason

    def install(self, job: ScheduledJob) -> Path:
        del job
        raise CapabilityUnavailableError(f"scheduling unavailable: {self._reason}")

    def remove(self, label: str) -> bool:
        del label
        return False

    def status(self, label: str) -> ScheduleStatus:
        return ScheduleStatus(label=label, installed=False, detail=self._reason)


def build() -> Platform:
    return Platform(
        name="generic",
        process=PosixProcessProbe() if os.name == "posix" else ConservativeProcessProbe(),
        notifier=LogNotifier(),
        keep_awake=NoKeepAwake(),
        placeholders=NoPlaceholders(),
        ocr=UnavailableOcr(),
        tts=UnavailableTts(),
        scheduler=UnavailableScheduler(),
        player=FfplayPlayer(),
    )
