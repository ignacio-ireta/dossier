"""Speaker review: clips, audio checks, answers, guided identification."""

from __future__ import annotations

import json
import math
import tomllib
import wave
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dossier.cli import app
from dossier.core.config import ProcessingStatus
from dossier.core.errors import DossierError
from dossier.core.policy import set_processing
from dossier.core.workspace import Workspace, open_workspace
from dossier.llm.fake import FakeProvider
from dossier.meet import people
from dossier.meet.models import DiarSegment, Word
from dossier.meet.pipeline import Engines, load_record, process_recording
from dossier.meet.review import (
    Answers,
    SpeakerAnswer,
    apply_answers,
    audio_report,
    grade_clip,
    load_answers,
    load_review,
    measure,
    pick_clips,
    review_dir,
)
from dossier.ops.sync import SyncOptions, run_sync
from dossier.platform import fake
from test_meet import SCRIPT, FakeAsr, FakeDiarizer, needs_ffmpeg

np = pytest.importorskip("numpy")
runner = CliRunner()
RATE = 16_000


def _speech_wav(path: Path, seconds: float = 17.0) -> Path:
    """Tone bursts (syllable-like on/off) where SCRIPT has speech, silence elsewhere."""
    t = np.arange(int(seconds * RATE)) / RATE
    signal = np.zeros_like(t)
    for start, end, _speaker, _text in SCRIPT:
        inside = (t >= start) & (t < end)
        gate = (np.floor(t / 0.2) % 2) == 0
        signal[inside & gate] = 0.3 * np.sin(2 * math.pi * 220 * t[inside & gate])
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes((signal * 32767).astype(np.int16).tobytes())
    return path


def _configure(workspace: Workspace, meet: str) -> Workspace:
    text = workspace.paths.config.read_text().replace("[meet]\n", f"[meet]\n{meet}\n", 1)
    text = text.replace("speaker_review = false", "").replace("speaker_voiceprints = false", "")
    workspace.paths.config.write_text(text)
    return open_workspace(workspace.root)


@pytest.fixture
def team(workspace: Workspace, fake_model: FakeProvider) -> Workspace:
    set_processing(workspace.paths.config, ProcessingStatus.APPROVED)
    return _configure(
        workspace,
        'people = ["Anne", "Beth", "Charlie", "Diego"]\n'
        "speaker_review = true\nspeaker_voiceprints = true",
    )


def _meeting(
    workspace: Workspace, tmp_path: Path, name: str = "2026-11-02 kickoff.wav", seconds: float = 17
) -> Path:
    recording = _speech_wav(tmp_path / name, seconds)  # another length: another recording
    record = process_recording(workspace, recording, engines=Engines(FakeAsr(), FakeDiarizer()))
    return workspace.paths.meetings / record.id


def _words(*spans: tuple[float, float]) -> list[Word]:
    return [Word(start=s, end=e, text=f"w{i}", confidence=0.9) for i, (s, e) in enumerate(spans)]


def test_pick_clips_prefers_clean_speech_spread_over_the_meeting() -> None:
    segments = [
        DiarSegment(start=0, end=12, speaker="A"),
        DiarSegment(start=10, end=14, speaker="B"),  # overlaps A from 10 to 12
        DiarSegment(start=30, end=36, speaker="A"),
        DiarSegment(start=60, end=61, speaker="A"),  # too short for a clip
    ]
    words = _words(*[(i * 0.5, i * 0.5 + 0.4) for i in range(24)], (30, 31), (31.5, 33), (34, 35.5))
    spans, clean = pick_clips("A", segments, words)
    assert clean == pytest.approx(10 + 6 + 1)
    assert [(round(s.start, 2), round(s.end, 2), s.overlap) for s in spans] == [
        (0.0, 10.0, False),  # stops where B starts talking over A
        (29.85, 35.65, False),  # a little before the segment: the first word is whole
    ]
    assert all(3 <= s.duration <= 10.3 for s in spans)
    # B only talks over A: its clip is marked as overlapping.
    b_spans, b_clean = pick_clips("B", segments, _words((10.2, 11), (11.5, 13.9)))
    assert b_clean == pytest.approx(2.0)
    assert [s.overlap for s in b_spans] == [True]


def test_clips_keep_the_first_word_but_not_the_next_voice() -> None:
    # Diarization starts A's segment after its first word ("I") and B right after A.
    segments = [
        DiarSegment(start=1.2, end=6.0, speaker="A"),
        DiarSegment(start=6.1, end=9, speaker="B"),
    ]
    words = [Word(start=1.0, end=1.15, text="I")] + [
        Word(start=1.3 + i * 0.5, end=1.7 + i * 0.5, text=f"w{i}") for i in range(9)
    ]
    words.append(Word(start=6.2, end=6.6, text="other"))
    [span], _clean = pick_clips("A", segments, words)
    assert span.start == pytest.approx(0.85) and span.end <= 6.1
    clip_words = [w.text for w in words if span.start <= (w.start + w.end) / 2 <= span.end]
    assert clip_words[0] == "I" and "other" not in clip_words


def test_measure_and_grade() -> None:
    t = np.arange(RATE * 4) / RATE
    speech = 0.3 * np.sin(2 * math.pi * 220 * t) * ((np.floor(t / 0.2) % 2) == 0)
    level, snr, clipping = measure(
        speech + 0.0005 * np.random.default_rng(1).standard_normal(len(t))
    )
    assert level > -20 and snr > 30 and clipping == 0
    grade, issues = grade_clip(
        duration=4, level_db=level, snr_db=snr, clipping=clipping, confidence=0.9, overlap=False
    )
    assert (grade, issues) == ("good", [])
    noisy = speech + 0.1 * np.random.default_rng(2).standard_normal(len(t))
    assert measure(noisy)[1] < 12
    loud = np.clip(speech * 5, -1, 1)
    assert measure(loud)[2] > 0.01
    grade, issues = grade_clip(
        duration=4, level_db=-50, snr_db=8, clipping=0.02, confidence=0.4, overlap=True
    )
    assert grade == "poor" and len(issues) == 5


def test_audio_report_overlap_and_unclear_words() -> None:
    segments = [
        DiarSegment(start=0, end=10, speaker="A"),
        DiarSegment(start=8, end=12, speaker="B"),
    ]
    words = [
        Word(start=i, end=i + 0.5, text="x", confidence=0.3 if i < 3 else 0.9) for i in range(10)
    ]
    report = audio_report(segments, words, [])
    assert report.overlap_ratio == pytest.approx(2 / 12, abs=0.001)
    assert report.unclear_words == pytest.approx(0.3)
    assert report.grade == "poor" and len(report.issues) == 2


@needs_ffmpeg
def test_review_built_by_the_pipeline_then_answers_applied(team: Workspace, tmp_path: Path) -> None:
    people.record_consent(team.paths, "Anne", "agreed in the kickoff, synthetic")
    folder = _meeting(team, tmp_path)
    record = load_record(folder)
    assert record.review_status == "pending" and record.stages["review"].status == "done"
    review = load_review(folder)
    assert review is not None
    assert review.names[:4] == ["Anne", "Beth", "Charlie", "Diego"]
    by_label = {s.label: s for s in review.speakers}
    assert [c.file for c in by_label["SPEAKER_00"].clips] == [
        "SPEAKER_00-1.wav",
        "SPEAKER_00-2.wav",
    ]
    assert by_label["SPEAKER_01"].clean_seconds == pytest.approx(3.5)
    assert "too little for a voiceprint" in " ".join(by_label["SPEAKER_01"].issues)
    first = by_label["SPEAKER_00"].clips[0]
    assert first.text.startswith("Good morning") and first.grade == "good"
    for speaker in review.speakers:
        for clip in speaker.clips:
            assert (review_dir(folder) / clip.file).is_file()
    page = (review_dir(folder) / "review.html").read_text()
    assert '<audio controls preload="none" src="SPEAKER_00-1.wav">' in page
    assert '<option value="Diego">' in page and "Save answers" in page
    template = load_answers(review_dir(folder) / "answers.toml")
    assert template.meeting == record.id and template.speakers["SPEAKER_00"].name == ""

    answers = Answers(
        meeting=record.id,
        audio="fair",
        speakers={
            "SPEAKER_00": SpeakerAnswer(name="Anne", quality="good"),
            "SPEAKER_01": SpeakerAnswer(name="Beth", quality="poor"),
        },
    )
    record, notes = apply_answers(team, folder, answers)
    assert {s.label: (s.name, s.status, s.quality) for s in record.speakers.values()} == {
        "SPEAKER_00": ("Anne", "confirmed", "good"),
        "SPEAKER_01": ("Beth", "confirmed", "poor"),
    }
    assert record.audio_quality == "fair" and record.review_status == "done"
    assert "voiceprint updated for Anne" in notes
    assert any("rated poor" in n for n in notes)
    assert any("review clips deleted" in n for n in notes)
    assert not list(review_dir(folder).glob("*.wav"))
    assert (review_dir(folder) / "review.json").is_file()  # the report stays
    stored = people.load(team.paths).people
    assert stored["Anne"].samples == 1 and "Beth" not in stored
    transcript = (folder / "transcript.md").read_text()
    assert "**[00:00] Anne:**" in transcript and "**[00:08] Beth:**" in transcript

    # The next meeting suggests Anne by voice; the review offers her name.
    second = _meeting(team, tmp_path, "2026-11-09 weekly.wav", seconds=18)
    next_review = load_review(second)
    assert next_review is not None
    suggested = {s.label: s.suggestion for s in next_review.speakers}
    assert suggested["SPEAKER_00"] == "Anne"


@needs_ffmpeg
def test_answers_are_checked(team: Workspace, tmp_path: Path) -> None:
    folder = _meeting(team, tmp_path)
    record = load_record(folder)
    with pytest.raises(DossierError, match="not"):
        apply_answers(team, folder, Answers(meeting="another-meeting"))
    with pytest.raises(DossierError, match="unknown speaker"):
        apply_answers(team, folder, Answers(meeting=record.id, speakers={"SPEAKER_09": {}}))
    bad = tmp_path / "bad.toml"
    bad.write_text(f'meeting = "{record.id}"\n[speakers.SPEAKER_00]\nnmae = "Anne"\n')
    with pytest.raises(DossierError, match="cannot read answers"):
        load_answers(bad)
    # What the review page's "Save answers" button writes.
    page_answers = tmp_path / f"answers-{record.id}.toml"
    page_answers.write_text(
        f'meeting = "{record.id}"\naudio = "good"\naudio_notes = ""\n\n'
        '[speakers.SPEAKER_00]\nname = "Charlie \\"CJ\\" Doe"\nquality = "good"\nnotes = ""\n\n'
        '[speakers.SPEAKER_01]\nname = ""\nquality = "fair"\nnotes = "echo"\n',
        encoding="utf-8",
    )
    record, _notes = apply_answers(team, folder, load_answers(page_answers))
    assert record.speakers["SPEAKER_00"].name == 'Charlie "CJ" Doe'
    assert record.speakers["SPEAKER_01"].status == "unknown"  # left for later
    assert record.review_status == "pending"
    assert list(review_dir(folder).glob("*.wav"))  # clips kept until everyone is named


@needs_ffmpeg
def test_identify_in_the_terminal(
    team: Workspace, tmp_path: Path, fake_platform: fake.FakeState
) -> None:
    folder = _meeting(team, tmp_path)
    ws = ["--workspace", str(team.root)]
    # SPEAKER_00: replay, then pick 1 (Anne), accept the audio grade.
    # SPEAKER_01: next clip (there is only one), type a new name, rate it poor.
    # Overall audio: accept.
    result = runner.invoke(
        app, [*ws, "meet", "identify", folder.name], input="r\n1\n\nn\nEve\np\n\n"
    )
    assert result.exit_code == 0, result.output
    assert [p.name for p in fake_platform.played] == [
        "SPEAKER_00-1.wav",
        "SPEAKER_00-1.wav",
        "SPEAKER_01-1.wav",
        "SPEAKER_01-1.wav",
    ]
    record = load_record(folder)
    assert record.speakers["SPEAKER_00"].name == "Anne"
    assert record.speakers["SPEAKER_01"].name == "Eve"
    assert record.speakers["SPEAKER_01"].quality == "poor"
    saved = tomllib.loads((review_dir(folder) / "answers.toml").read_text())
    assert saved["speakers"]["SPEAKER_01"] == {"name": "Eve", "quality": "poor", "notes": ""}
    result = runner.invoke(app, ["--json", *ws, "meet", "identify", folder.name])
    assert result.exit_code != 0  # interactive only without --json


@needs_ffmpeg
def test_review_command_and_list(
    team: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _meeting(team, tmp_path)
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    ws = ["--workspace", str(team.root)]
    result = runner.invoke(app, ["--json", *ws, "meet", "review", folder.name, "--open"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["page"].endswith("review/review.html") and len(data["speakers"]) == 2
    assert opened and opened[0].startswith("file://")
    result = runner.invoke(app, ["--json", *ws, "meet", "list"])
    assert json.loads(result.stdout)[0]["to_identify"] == 2


@needs_ffmpeg
def test_sync_notes_meetings_to_identify(team: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    engines = Engines(FakeAsr(), FakeDiarizer())
    monkeypatch.setattr("dossier.meet.pipeline.default_engines", lambda ws, diarize=True: engines)
    monkeypatch.setattr("dossier.meet.collect.meet_extra_available", lambda: True)
    _speech_wav(team.paths.sources / "calls" / "2026-11-03 planning.wav")
    outcome = run_sync(team, SyncOptions(stages=["convert", "meetings"]))
    meetings = outcome.stages["meetings"]
    assert meetings["counts"]["to_identify"] == 1
    assert "speaker(s) to name; run `dsr meet identify" in meetings["notes"][0]
    assert outcome.exit_status == 0  # a pending review is not a failure
