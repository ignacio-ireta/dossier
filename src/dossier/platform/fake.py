"""In-memory platform for tests: records calls, needs no OS integration."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from dossier.platform.base import OcrLine, Platform, ScheduledJob, ScheduleStatus


@dataclass
class FakeState:
    dead_pids: set[int] = field(default_factory=set)
    placeholders: set[Path] = field(default_factory=set)
    notifications: list[tuple[str, str]] = field(default_factory=list)
    holds: list[str] = field(default_factory=list)
    jobs: dict[str, ScheduledJob] = field(default_factory=dict)
    ocr_text: dict[Path, list[str]] = field(default_factory=dict)
    played: list[Path] = field(default_factory=list)


class _Base:
    name = "fake"

    def __init__(self, state: FakeState) -> None:
        self.state = state

    def available(self) -> bool:
        return True

    def detail(self) -> str:
        return "fake"


class FakeProcess(_Base):
    name = "process"

    def pid_alive(self, pid: int) -> bool:
        return pid not in self.state.dead_pids


class FakeNotifier(_Base):
    name = "notify"

    def notify(self, title: str, message: str) -> None:
        self.state.notifications.append((title, message))


class FakeKeepAwake(_Base):
    name = "keep_awake"

    @contextmanager
    def hold(self, reason: str) -> Iterator[None]:
        self.state.holds.append(reason)
        yield


class FakePlaceholders(_Base):
    name = "cloud_placeholders"

    def is_placeholder(self, path: Path) -> bool:
        return path in self.state.placeholders


class FakeOcr(_Base):
    name = "ocr"

    def recognize(self, image: Path, *, languages: list[str] | None = None) -> list[OcrLine]:
        del languages
        return [OcrLine(text, 0.99) for text in self.state.ocr_text.get(image, [])]


class FakeTts(_Base):
    name = "tts"

    def voices(self) -> list[str]:
        return ["fake-a", "fake-b"]

    def synthesize(self, text: str, output: Path, *, voice: str | None = None) -> None:
        output.write_text(f"{voice or 'default'}:{text}", encoding="utf-8")


class FakePlayer(_Base):
    name = "player"

    def play(self, path: Path) -> None:
        self.state.played.append(path)


class FakeScheduler(_Base):
    name = "scheduler"

    def install(self, job: ScheduledJob) -> Path:
        self.state.jobs[job.label] = job
        return job.working_dir / f"{job.label}.job"

    def remove(self, label: str) -> bool:
        return self.state.jobs.pop(label, None) is not None

    def status(self, label: str) -> ScheduleStatus:
        return ScheduleStatus(label=label, installed=label in self.state.jobs)


def build(state: FakeState | None = None) -> Platform:
    state = state or FakeState()
    return Platform(
        name="fake",
        process=FakeProcess(state),
        notifier=FakeNotifier(state),
        keep_awake=FakeKeepAwake(state),
        placeholders=FakePlaceholders(state),
        ocr=FakeOcr(state),
        tts=FakeTts(state),
        scheduler=FakeScheduler(state),
        player=FakePlayer(state),
    )
