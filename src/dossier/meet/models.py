"""Data models for meetings."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Word(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    speaker: str | None = None
    confidence: float | None = None


class DiarSegment(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    speaker: str


class Turn(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    speaker: str
    text: str


class SpeakerInfo(BaseModel):
    label: str  # SPEAKER_00 ...
    seconds: float = 0.0
    name: str | None = None
    status: Literal["unknown", "suggested", "confirmed"] = "unknown"
    confidence: float | None = None

    def display(self) -> str:
        if self.status == "confirmed" and self.name:
            return self.name
        if self.status == "suggested" and self.name:
            conf = f", {self.confidence:.2f}" if self.confidence is not None else ""
            return f"{self.label} (likely {self.name}{conf})"
        return self.label


class StageRecord(BaseModel):
    status: Literal["done", "skipped", "failed"]
    version: int
    finished_at: str
    detail: str = ""


class MeetingRecord(BaseModel):
    schema_version: Literal[1] = 1
    id: str
    title: str
    date: str  # YYYY-MM-DD
    source: str  # where the recording was (workspace-relative when inside it)
    sha256: str
    duration_s: float | None = None
    language: str = "en"
    asr: str = ""
    diarization: str = ""
    roster: list[str] = Field(default_factory=list)
    speakers: dict[str, SpeakerInfo] = Field(default_factory=dict)
    stages: dict[str, StageRecord] = Field(default_factory=dict)
    deck: str | None = None
    summary_status: Literal["none", "ok", "degraded"] = "none"


def clock(seconds: float) -> str:
    whole = round(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def parse_clock(value: str) -> float | None:
    parts = value.strip().strip("[]").split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    numbers = [int(p) for p in parts]
    if len(numbers) == 2:
        return float(numbers[0] * 60 + numbers[1])
    return float(numbers[0] * 3600 + numbers[1] * 60 + numbers[2])
