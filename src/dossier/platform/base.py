"""Interfaces for every OS-specific capability.

Pipelines depend only on these protocols. Each OS module (``macos``,
``windows``, ``generic``) supplies a :class:`Platform` bundle; the rest of the
code base never calls an OS-specific program or API directly. A capability
without a backend on the current OS reports ``available() == False`` and raises
:class:`~dossier.core.errors.CapabilityUnavailableError` when used.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class Capability(Protocol):
    name: str

    def available(self) -> bool: ...

    def detail(self) -> str:
        """Short human-readable description of the backend or why it is missing."""
        ...


class ProcessProbe(Capability, Protocol):
    def pid_alive(self, pid: int) -> bool:
        """True if a process with this pid may be alive. Unknown must answer True."""
        ...


class Notifier(Capability, Protocol):
    def notify(self, title: str, message: str) -> None: ...


class KeepAwake(Capability, Protocol):
    def hold(self, reason: str) -> AbstractContextManager[None]:
        """Keep the machine from idle-sleeping while the context is open."""
        ...


class CloudPlaceholders(Capability, Protocol):
    def is_placeholder(self, path: Path) -> bool:
        """True if the file is a cloud-only stub whose bytes are not on disk."""
        ...


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float
    # Normalized bounding box (x, y, width, height), origin top-left, 0..1.
    box: tuple[float, float, float, float] | None = None


class Ocr(Capability, Protocol):
    def recognize(self, image: Path, *, languages: list[str] | None = None) -> list[OcrLine]: ...


class Tts(Capability, Protocol):
    def voices(self) -> list[str]: ...

    def synthesize(self, text: str, output: Path, *, voice: str | None = None) -> None:
        """Render speech to ``output``; the suffix (.wav/.m4a/.aiff) picks the format."""
        ...


class AudioPlayer(Capability, Protocol):
    def play(self, path: Path) -> None:
        """Play an audio file through the speakers; returns when it has finished."""
        ...


@dataclass(frozen=True)
class ScheduledJob:
    label: str
    program: list[str]
    working_dir: Path
    log_path: Path
    daily_at: tuple[int, int] | None = None  # (hour, minute) local time
    watch_paths: list[Path] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ScheduleStatus:
    label: str
    installed: bool
    detail: str = ""


class Scheduler(Capability, Protocol):
    def install(self, job: ScheduledJob) -> Path:
        """Install or replace the job; returns the definition file written."""
        ...

    def remove(self, label: str) -> bool: ...

    def status(self, label: str) -> ScheduleStatus: ...


@dataclass(frozen=True)
class Platform:
    name: str
    process: ProcessProbe
    notifier: Notifier
    keep_awake: KeepAwake
    placeholders: CloudPlaceholders
    ocr: Ocr
    tts: Tts
    scheduler: Scheduler
    player: AudioPlayer

    def capabilities(self) -> Iterator[Capability]:
        yield from (
            self.process,
            self.notifier,
            self.keep_awake,
            self.placeholders,
            self.ocr,
            self.tts,
            self.scheduler,
            self.player,
        )
