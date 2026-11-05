from __future__ import annotations

import asyncio
import json
import shutil
import wave
from pathlib import Path
from typing import Any

import pytest

from dossier.core.config import ProcessingStatus
from dossier.core.errors import DossierError
from dossier.core.policy import set_processing
from dossier.core.workspace import Workspace, open_workspace
from dossier.llm.fake import FakeProvider
from dossier.meet import people
from dossier.meet.backends import Diarization
from dossier.meet.eval import loops, score
from dossier.meet.glossary import Glossary
from dossier.meet.models import DiarSegment, SpeakerInfo, Word, clock, parse_clock
from dossier.meet.pipeline import (
    Engines,
    MeetOptions,
    confirm_speakers,
    list_meetings,
    load_record,
    process_recording,
)
from dossier.meet.speakers import assign, suggest_names, to_turns
from dossier.ops.sync import SyncOptions, run_sync

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

SCRIPT = [
    (0.0, 4.0, "SPEAKER_00", "Good morning, let's review the rollout."),
    (4.5, 8.0, "SPEAKER_00", "We keep the Lakeside region for hosting."),
    (8.5, 12.0, "SPEAKER_01", "This is Sam. I will prepare the migration checklist by Friday."),
    (12.5, 16.0, "SPEAKER_00", "Who approves the budget?"),
]


class FakeAsr:
    name = "fake-asr"
    model_id = "fake"

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str | None] = []

    def transcribe(self, audio: Path, *, language: str | None, prompt: str | None) -> list[Word]:
        self.calls += 1
        self.prompts.append(prompt)
        words: list[Word] = []
        for start, end, _speaker, text in SCRIPT:
            tokens = text.split()
            step = (end - start) / len(tokens)
            words += [
                Word(start=start + i * step, end=start + (i + 1) * step, text=t)
                for i, t in enumerate(tokens)
            ]
        return words


class FakeDiarizer:
    name = "fake-diarizer"

    def __init__(self, embeddings: dict[str, list[float]] | None = None) -> None:
        self.calls = 0
        self.embeddings = embeddings or {
            "SPEAKER_00": [1.0, 0.0, 0.0],
            "SPEAKER_01": [0.0, 1.0, 0.0],
        }

    def diarize(self, audio: Path) -> Diarization:
        self.calls += 1
        return Diarization(
            segments=[DiarSegment(start=s, end=e, speaker=who) for s, e, who, _ in SCRIPT],
            embeddings=self.embeddings,
        )


def _wav(path: Path, seconds: float = 2.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


@pytest.fixture
def approved(workspace: Workspace, fake_model: FakeProvider) -> Workspace:
    set_processing(workspace.paths.config, ProcessingStatus.APPROVED)
    return open_workspace(workspace.root)


@needs_ffmpeg
def test_process_recording_with_fake_engines(approved: Workspace, tmp_path: Path) -> None:
    asr, diarizer = FakeAsr(), FakeDiarizer()
    recording = _wav(tmp_path / "2026-11-02 rollout review.wav")
    record = process_recording(approved, recording, MeetOptions(), engines=Engines(asr, diarizer))
    assert record.id.startswith("2026-11-02-rollout-review-")
    assert record.date == "2026-11-02" and record.title == "Rollout review"
    assert set(record.speakers) == {"SPEAKER_00", "SPEAKER_01"}
    assert all(
        s.status == "done" for k, s in record.stages.items() if k not in {"identify", "deck"}
    )
    folder = approved.paths.meetings / record.id
    transcript = (folder / "transcript.md").read_text()
    assert "**[00:08] SPEAKER_01:** This is Sam." in transcript
    summary = (folder / "summary.md").read_text()
    assert "Keep the Lakeside region [00:05]" in summary
    assert "Invented decision" not in summary  # its time is not in the transcript
    actions = json.loads((folder / "actions.json").read_text())
    by_task = {a["task"]: a for a in actions}
    assert by_task["Prepare the migration checklist"]["owner"] == "Sam"
    assert by_task["Prepare the migration checklist"]["due"] == "Friday"
    assert by_task["Book the venue"]["owner"] == "UNKNOWN"  # never said in the meeting
    assert by_task["Book the venue"]["due"] == "UNKNOWN"
    assert not (folder / "machine" / "audio.wav").exists()  # audio cache removed
    assert (folder / "machine" / "transcript.srt").read_text().startswith("1\n00:00:00,000")


@needs_ffmpeg
def test_resume_and_force(approved: Workspace, tmp_path: Path) -> None:
    asr, diarizer = FakeAsr(), FakeDiarizer()
    recording = _wav(tmp_path / "standup.wav")
    first = process_recording(approved, recording, engines=Engines(asr, diarizer))
    second = process_recording(approved, recording, engines=Engines(asr, diarizer))
    assert first.id == second.id and asr.calls == 1 and diarizer.calls == 1
    process_recording(
        approved, recording, MeetOptions(force={"asr"}), engines=Engines(asr, diarizer)
    )
    assert asr.calls == 2 and diarizer.calls == 2  # later stages re-run too
    assert len(list_meetings(approved)) == 1


@needs_ffmpeg
def test_glossary_prompt_and_replacements(approved: Workspace, tmp_path: Path) -> None:
    (approved.root / "glossary.toml").write_text(
        'terms = ["Lakeside", "DataOps"]\n[replacements]\n"lakeside region" = "Lakeside Region"\n'
    )
    text = approved.paths.config.read_text().replace(
        "[meet]\n", '[meet]\nglossary = "glossary.toml"\n'
    )
    approved.paths.config.write_text(text)
    workspace = open_workspace(approved.root)
    asr = FakeAsr()
    record = process_recording(
        workspace, _wav(tmp_path / "x.wav"), engines=Engines(asr, FakeDiarizer())
    )
    assert asr.prompts[0] == "Glossary: Lakeside, DataOps."
    words = json.loads(
        (workspace.paths.meetings / record.id / "machine" / "words.json").read_text()
    )
    assert "Lakeside Region" not in " ".join(w["text"] for w in words)  # word-level, see below
    glossary = Glossary.load(workspace.root / "glossary.toml")
    assert glossary.apply("we keep the LAKESIDE REGION") == "we keep the Lakeside Region"


def test_speaker_assignment_and_turns() -> None:
    words = [
        Word(start=0.0, end=0.5, text="hi"),
        Word(start=0.6, end=1.0, text="there"),
        Word(start=3.0, end=3.5, text="hello"),
    ]
    segments = [
        DiarSegment(start=0, end=1.2, speaker="A"),
        DiarSegment(start=2.8, end=4, speaker="B"),
    ]
    turns = to_turns(assign(words, segments))
    assert [(t.speaker, t.text) for t in turns] == [("A", "hi there"), ("B", "hello")]
    assert clock(3725) == "1:02:05" and parse_clock("1:02:05") == 3725.0
    assert parse_clock("[00:18]") == 18.0 and parse_clock("soon") is None


def test_suggestions_are_greedy_thresholded_and_respect_roster() -> None:
    speakers = {"S0": SpeakerInfo(label="S0"), "S1": SpeakerInfo(label="S1")}
    embeddings = {"S0": [1.0, 0.0], "S1": [0.0, 1.0]}
    prints = {"Alex": [0.99, 0.1], "Sam": [0.1, 0.99], "Kim": [0.98, 0.15]}
    result = suggest_names(speakers, embeddings, prints, threshold=0.6)
    assert result["S0"].name == "Alex" and result["S0"].status == "suggested"
    assert result["S1"].name == "Sam"
    roster = suggest_names(speakers, embeddings, prints, threshold=0.6, roster=["Kim"])
    assert roster["S0"].name == "Kim" and roster["S1"].status == "unknown"
    strict = suggest_names(speakers, embeddings, prints, threshold=0.999)
    assert all(info.status == "unknown" for info in strict.values())
    assert result["S0"].display().startswith("S0 (likely Alex,")


@needs_ffmpeg
def test_voiceprint_consent_enroll_suggest_forget(approved: Workspace, tmp_path: Path) -> None:
    text = approved.paths.config.read_text().replace(
        "speaker_voiceprints = false", "speaker_voiceprints = true"
    )
    approved.paths.config.write_text(text)
    workspace = open_workspace(approved.root)
    record = process_recording(
        workspace, _wav(tmp_path / "a.wav"), engines=Engines(FakeAsr(), FakeDiarizer())
    )
    folder = workspace.paths.meetings / record.id
    _, notes = confirm_speakers(workspace, folder, {"SPEAKER_01": "Sam"})
    assert notes == ["no recorded consent for 'Sam'; run `dsr people consent 'Sam'` first"]
    assert "**[00:08] Sam:**" in (folder / "transcript.md").read_text()
    people.record_consent(workspace.paths, "Sam", "synthetic test")
    _, notes = confirm_speakers(workspace, folder, {"SPEAKER_01": "Sam"})
    assert notes == ["voiceprint updated for Sam"]
    other = process_recording(
        workspace, _wav(tmp_path / "b.wav", seconds=3.0), engines=Engines(FakeAsr(), FakeDiarizer())
    )
    suggested = load_record(workspace.paths.meetings / other.id).speakers["SPEAKER_01"]
    assert (suggested.name, suggested.status) == ("Sam", "suggested")
    assert (
        "(likely Sam, 1.00)" in (workspace.paths.meetings / other.id / "transcript.md").read_text()
    )
    assert people.forget(workspace.paths, "Sam")
    assert people.voiceprints(workspace.paths, workspace.config.meet.diarization_model) == {}
    with pytest.raises(DossierError):
        confirm_speakers(workspace, folder, {"SPEAKER_09": "Nobody"})


def test_eval_scoring() -> None:
    glossary = Glossary(terms=["Lakeside", "DataOps"], replacements={"data ops": "DataOps"})
    result = score("We use data ops near Lakeside " + "uh " * 7, glossary)
    assert result.terms_found == ["Lakeside"] and result.bad_forms == ["data ops"]
    assert result.loops == [("uh", 7)]
    assert result.score == round(100 * 0.5 - 5 - 0.5 * 7, 2)
    assert loops("no repetition here") == []


@needs_ffmpeg
def test_meetings_indexed_searchable_and_mcp(approved: Workspace, tmp_path: Path) -> None:
    from mcp import Client

    from dossier.agent.mcp_server import build_server

    record = process_recording(
        approved,
        _wav(tmp_path / "2026-11-02 review.wav"),
        engines=Engines(FakeAsr(), FakeDiarizer()),
    )
    outcome = run_sync(approved, SyncOptions(stages=["index"]))
    assert outcome.exit_status == 0, outcome.stages
    from dossier.index.search import SearchFilters, search

    hits, _ = search(approved, "migration checklist", filters=SearchFilters(kind="meeting"))
    assert hits and hits[0].doc.startswith(f"meetings/{record.id}/")

    async def session() -> dict[str, Any]:
        async with Client(build_server(approved.root)) as client:
            out = {}
            for name, args in (
                ("meetings", {}),
                ("transcript", {"meeting": record.id, "start": "00:09", "end": "00:10"}),
                ("actions", {"owner": "sam"}),
            ):
                result = await client.call_tool(name, args)
                out[name] = result.structured_content
            return out

    out = asyncio.run(session())
    assert out["meetings"]["meetings"][0]["id"] == record.id
    assert [t["speaker"] for t in out["transcript"]["turns"]] == ["SPEAKER_01"]
    assert out["actions"]["actions"][0]["task"] == "Prepare the migration checklist"


@needs_ffmpeg
def test_sync_stage_transcribes_media_in_sources(
    approved: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    engines = Engines(FakeAsr(), FakeDiarizer())
    monkeypatch.setattr("dossier.meet.pipeline.default_engines", lambda ws, diarize=True: engines)
    monkeypatch.setattr("dossier.meet.collect.meet_extra_available", lambda: True)
    _wav(approved.paths.sources / "calls" / "2026-11-03 planning.wav")
    outcome = run_sync(approved, SyncOptions(stages=["convert", "meetings"]))
    assert outcome.stages["convert"]["main"]["counts"]["media"] == 1
    assert outcome.stages["meetings"]["counts"] == {
        "recordings_new": 1,
        "transcribed": 1,
        "failed": 0,
    }
    again = run_sync(approved, SyncOptions(stages=["meetings"]))
    assert again.stages["meetings"]["counts"]["recordings_new"] == 0


def test_sync_stage_without_meet_extra(
    approved: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dossier.meet.collect.meet_extra_available", lambda: False)
    _wav(approved.paths.sources / "call.wav")
    outcome = run_sync(approved, SyncOptions(stages=["meetings"]))
    assert outcome.exit_status == 0
    assert "install the meet extra" in outcome.stages["meetings"]["notes"][0]
