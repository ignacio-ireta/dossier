"""Speaker review: short clips per speaker, an audio check, and names from a person.

Diarization tells voices apart; only a person can say whose they are. This
module keeps that step to a few minutes:

1. :func:`build_review` picks up to three clips for each unidentified speaker
   (3-10 seconds in which only that speaker talks, spread over the meeting),
   cuts them from the original recording and measures each one: loudness,
   background noise, distortion (clipping) and how sure the recognizer was of
   the words. Everything goes to ``meetings/<id>/review/``: the clips,
   ``review.json``, ``answers.toml`` to fill in, and ``review.html``, a page
   that plays the clips and saves the answers file.
2. A person listens and answers who each speaker is and how the audio sounds
   (``dsr meet identify`` asks in the terminal instead).
3. :func:`apply_answers` confirms the names. A voiceprint is stored only for
   people with recorded consent, and never from audio rated poor or from too
   little clean speech. The clips are deleted once every speaker is named.
"""

from __future__ import annotations

import html
import json
import logging
import math
import shutil
import tempfile
import tomllib
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dossier.core.errors import DossierError
from dossier.core.io import atomic_write_json, atomic_write_text, read_json, sha256_file
from dossier.core.workspace import Workspace
from dossier.meet import people as people_store
from dossier.meet.audio import SAMPLE_RATE, normalize
from dossier.meet.models import (
    DiarSegment,
    Grade,
    MeetingRecord,
    SpeakerInfo,
    Turn,
    Word,
    clock,
)

log = logging.getLogger("dossier.meet")

MIN_CLIP = 3.0  # seconds
MAX_CLIP = 10.0
PAD = 0.15  # seconds of context around the first and last word
MIN_VOICEPRINT_SECONDS = 8.0  # clean speech needed before a voiceprint is stored
REVIEW_DIR = "review"
_GRADES: tuple[Grade, ...] = ("good", "fair", "poor")


class ClipReport(BaseModel):
    file: str
    start: float
    end: float
    text: str
    overlap: bool = False  # other voices during the clip
    level_db: float  # loudness of the speech, dBFS
    snr_db: float  # speech over background noise
    clipping: float  # share of distorted (clipped) samples
    confidence: float | None = None  # recognizer's mean word confidence
    grade: Grade
    issues: list[str] = Field(default_factory=list)


class SpeakerReport(BaseModel):
    label: str
    suggestion: str | None = None
    confidence: float | None = None
    confirmed: str | None = None
    seconds: float
    clean_seconds: float  # speech without other voices
    grade: Grade
    issues: list[str] = Field(default_factory=list)
    clips: list[ClipReport] = Field(default_factory=list)


class AudioReport(BaseModel):
    grade: Grade
    issues: list[str] = Field(default_factory=list)
    overlap_ratio: float  # share of speech time with two or more voices
    unclear_words: float | None = None  # share of words the recognizer was unsure of


class Review(BaseModel):
    schema_version: Literal[1] = 1
    meeting: str
    title: str
    date: str
    created_at: str
    names: list[str] = Field(default_factory=list)  # choices for the reviewer
    audio: AudioReport
    speakers: list[SpeakerReport] = Field(default_factory=list)


class SpeakerAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = ""
    quality: Grade | None = None
    notes: str = ""


class Answers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meeting: str
    audio: Grade | None = None
    audio_notes: str = ""
    speakers: dict[str, SpeakerAnswer] = Field(default_factory=dict)


# --- choosing clips ----------------------------------------------------------


@dataclass(frozen=True)
class Span:
    start: float
    end: float
    overlap: bool

    @property
    def duration(self) -> float:
        return self.end - self.start


def _merge(intervals: list[tuple[float, float]], gap: float = 0.0) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if merged and start - merged[-1][1] <= gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _subtract(
    intervals: list[tuple[float, float]], others: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for start, end in intervals:
        pieces = [(start, end)]
        for o_start, o_end in others:
            next_pieces = []
            for p_start, p_end in pieces:
                if o_end <= p_start or o_start >= p_end:
                    next_pieces.append((p_start, p_end))
                    continue
                if o_start > p_start:
                    next_pieces.append((p_start, o_start))
                if o_end < p_end:
                    next_pieces.append((o_end, p_end))
            pieces = next_pieces
        result.extend(p for p in pieces if p[1] - p[0] > 0.05)
    return result


SLACK = 0.5  # diarization segments start and end a little inside the speech


def _windows(
    interval: tuple[float, float],
    words: list[Word],
    overlap: bool,
    others: list[tuple[float, float]],
) -> list[Span]:
    """Word-aligned windows of MIN_CLIP..MAX_CLIP seconds over an interval's words.

    A window may reach up to SLACK seconds past the interval, so the first and
    last words are not cut off, but (for clean windows) never into another voice.
    """
    start, end = interval
    low, high = max(0.0, start - SLACK), end + SLACK
    if not overlap:
        for other_start, other_end in others:
            if other_end <= start + 1e-6:
                low = max(low, other_end)
            if other_start >= end - 1e-6:
                high = min(high, other_start)
    inside = [w for w in words if low <= (w.start + w.end) / 2 <= high]
    spans: list[Span] = []
    index = 0
    while index < len(inside):
        first = inside[index]
        last_index = index
        while last_index + 1 < len(inside) and inside[last_index + 1].end - first.start <= MAX_CLIP:
            last_index += 1
        last = inside[last_index]
        if last.end - first.start >= MIN_CLIP:
            spans.append(Span(max(low, first.start - PAD), min(high, last.end + PAD), overlap))
        index = last_index + 1
    return spans


def _span_words(span: Span, words: list[Word]) -> list[Word]:
    return [w for w in words if span.start <= (w.start + w.end) / 2 <= span.end]


def _score(span: Span, words: list[Word]) -> float:
    inside = _span_words(span, words)
    confidences = [w.confidence for w in inside if w.confidence is not None]
    confidence = sum(confidences) / len(confidences) if confidences else 0.8
    return len(inside) * confidence * min(1.0, span.duration / 6.0) * (0.5 if span.overlap else 1)


def pick_clips(
    label: str, segments: list[DiarSegment], words: list[Word], count: int = 3
) -> tuple[list[Span], float]:
    """Up to ``count`` clips for one speaker, and their seconds of clean speech."""
    mine = [(s.start, s.end) for s in segments if s.speaker == label]
    own = _merge(mine, gap=0.5)  # short pauses do not split a clip
    others = _merge([(s.start, s.end) for s in segments if s.speaker != label])
    clean = _subtract(own, others)
    clean_seconds = sum(end - start for start, end in _subtract(_merge(mine), others))
    candidates = [span for part in clean for span in _windows(part, words, False, others)]
    if not candidates:  # no stretch without other voices: accept overlap
        candidates = [span for part in own for span in _windows(part, words, True, others)]
    if not candidates and own:  # very little speech: the longest piece there is
        start, end = max(own, key=lambda i: i[1] - i[0])
        candidates = [Span(start, end, bool(_subtract([(start, end)], others) != [(start, end)]))]
    if not candidates:
        return [], round(clean_seconds, 1)
    first, last = own[0][0], own[-1][1]
    part = max(1e-6, (last - first) / count)
    chosen: list[Span] = []
    for number in range(count):  # the best clip from each part of the meeting
        low, high = first + number * part, first + (number + 1) * part
        pool = [
            c for c in candidates if low <= (c.start + c.end) / 2 < high + 1e-6 and c not in chosen
        ]
        if pool:
            chosen.append(max(pool, key=lambda c: _score(c, words)))
    for span in sorted(candidates, key=lambda c: _score(c, words), reverse=True):
        if len(chosen) >= count:
            break
        if span not in chosen:
            chosen.append(span)
    return sorted(chosen, key=lambda c: c.start), round(clean_seconds, 1)


# --- measuring audio ---------------------------------------------------------


def measure(samples: Any) -> tuple[float, float, float]:
    """(speech level dBFS, signal-to-noise dB, share of clipped samples) of mono samples."""
    import numpy as np

    data = np.asarray(samples, dtype=np.float32)
    frame = int(0.03 * SAMPLE_RATE)
    count = len(data) // frame
    if count < 2:
        return -90.0, 0.0, 0.0
    rms = np.sqrt(np.mean(np.square(data[: count * frame].reshape(count, frame)), axis=1)) + 1e-9
    floor, peak = float(np.percentile(rms, 10)), float(np.percentile(rms, 90))
    snr = max(0.0, min(60.0, 20 * math.log10(peak / floor)))
    active = rms[rms >= floor * 2] if np.any(rms >= floor * 2) else rms
    level = 20 * math.log10(float(np.sqrt(np.mean(np.square(active)))))
    clipping = float(np.mean(np.abs(data) >= 0.99))
    return round(level, 1), round(snr, 1), round(clipping, 4)


def grade_clip(
    *,
    duration: float,
    level_db: float,
    snr_db: float,
    clipping: float,
    confidence: float | None,
    overlap: bool,
) -> tuple[Grade, list[str]]:
    issues: list[str] = []
    poor = fair = False
    if snr_db < 10:
        issues.append(f"noisy (speech only {snr_db:.0f} dB above the background)")
        poor = True
    elif snr_db < 20:
        issues.append(f"some background noise ({snr_db:.0f} dB)")
        fair = True
    if level_db < -45:
        issues.append(f"very quiet ({level_db:.0f} dBFS)")
        poor = True
    elif level_db < -35:
        issues.append(f"quiet ({level_db:.0f} dBFS)")
        fair = True
    if clipping > 0.01:
        issues.append(f"distorted ({clipping:.1%} of samples clipped)")
        poor = True
    elif clipping > 0.001:
        issues.append("slightly distorted (clipping)")
        fair = True
    if confidence is not None and confidence < 0.5:
        issues.append(f"words hard to make out (recognizer confidence {confidence:.2f})")
        poor = True
    elif confidence is not None and confidence < 0.75:
        issues.append(f"some unclear words (recognizer confidence {confidence:.2f})")
        fair = True
    if overlap:
        issues.append("other voices overlap")
        fair = True
    if duration < MIN_CLIP:
        issues.append(f"short ({duration:.1f} s)")
        fair = True
    return ("poor" if poor else "fair" if fair else "good"), issues


def _overlap_ratio(segments: list[DiarSegment]) -> float:
    events = sorted([(s.start, 1) for s in segments] + [(s.end, -1) for s in segments])
    active = 0
    previous = 0.0
    speech = overlapped = 0.0
    for time, change in events:
        if active >= 1:
            speech += time - previous
        if active >= 2:
            overlapped += time - previous
        active += change
        previous = time
    return round(overlapped / speech, 3) if speech else 0.0


def audio_report(
    segments: list[DiarSegment], words: list[Word], speakers: list[SpeakerReport]
) -> AudioReport:
    overlap = _overlap_ratio(segments)
    confidences = [w.confidence for w in words if w.confidence is not None]
    unclear = (
        round(sum(1 for c in confidences if c < 0.5) / len(confidences), 3) if confidences else None
    )
    issues: list[str] = []
    grade: Grade = "good"
    if overlap > 0.25:
        issues.append(f"{overlap:.0%} of the speech has several voices at once")
        grade = "poor"
    elif overlap > 0.10:
        issues.append(f"{overlap:.0%} of the speech has several voices at once")
        grade = "fair"
    if unclear is not None and unclear > 0.25:
        issues.append(f"{unclear:.0%} of the words were hard to make out")
        grade = "poor"
    elif unclear is not None and unclear > 0.10:
        issues.append(f"{unclear:.0%} of the words were hard to make out")
        grade = "fair" if grade == "good" else grade
    poor_speakers = [s.label for s in speakers if s.grade == "poor"]
    if poor_speakers:
        issues.append("poor audio for " + ", ".join(poor_speakers))
        grade = "fair" if grade == "good" else grade
    return AudioReport(grade=grade, issues=issues, overlap_ratio=overlap, unclear_words=unclear)


# --- building the review -----------------------------------------------------


def review_dir(folder: Path) -> Path:
    return folder / REVIEW_DIR


def load_review(folder: Path) -> Review | None:
    path = review_dir(folder) / "review.json"
    return Review.model_validate(read_json(path)) if path.is_file() else None


def recording_path(workspace: Workspace, record: MeetingRecord, override: Path | None) -> Path:
    """The meeting's original recording, checked against its hash when moved."""
    if override is not None:
        if not override.is_file():
            raise DossierError(f"{override} not found")
        if sha256_file(override) != record.sha256:
            raise DossierError(f"{override} is not the recording of meeting {record.id}")
        return override
    path = Path(record.source)
    path = path if path.is_absolute() else workspace.root / path
    if not path.is_file():
        raise DossierError(
            f"the recording is no longer at {record.source}; pass --recording <file>"
        )
    return path


def candidate_names(workspace: Workspace, record: MeetingRecord) -> list[str]:
    """Names to offer: the team list, this meeting's attendees, people with consent,
    and names confirmed in other meetings."""
    from dossier.meet.pipeline import list_meetings

    names = [*workspace.config.meet.people, *record.roster]
    names += list(people_store.load(workspace.paths).people)
    for other in list_meetings(workspace):
        names += [s.name for s in other.speakers.values() if s.status == "confirmed" and s.name]
    return list(dict.fromkeys(n.strip() for n in names if n and n.strip()))


def _read_clip(handle: wave.Wave_read, start: float, end: float) -> Any:
    import numpy as np

    rate = handle.getframerate()
    first = max(0, int(start * rate))
    handle.setpos(min(first, handle.getnframes()))
    frames = handle.readframes(max(0, int((end - start) * rate)))
    return np.frombuffer(frames, dtype=np.int16)


def _write_clip(path: Path, samples: Any) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(samples.tobytes())


def build_review(
    workspace: Workspace,
    folder: Path,
    *,
    recording: Path | None = None,
    include_confirmed: bool = False,
    clips_per_speaker: int = 3,
) -> Review | None:
    """Write the review folder; None when every speaker is already confirmed."""
    from dossier.meet.pipeline import load_record, save_record

    record = load_record(folder)
    labels = [
        label
        for label, info in record.speakers.items()
        if include_confirmed or info.status != "confirmed"
    ]
    if not labels:
        return None
    machine = folder / "machine"
    words = [Word(**w) for w in read_json(machine / "words.json")]
    diar = read_json(machine / "diarization.json")
    segments = [DiarSegment(**s) for s in diar.get("segments", [])]
    if not segments:  # diarization off: everything is one speaker
        turns = [Turn(**t) for t in read_json(machine / "turns.json")]
        segments = [DiarSegment(start=t.start, end=t.end, speaker=t.speaker) for t in turns]
    source = recording_path(workspace, record, recording)
    target = review_dir(folder)
    target.mkdir(parents=True, exist_ok=True)
    for old in target.glob("*.wav"):
        old.unlink()
    reports: list[SpeakerReport] = []
    with tempfile.TemporaryDirectory(prefix="dossier-review-") as temporary:
        raw = Path(temporary) / "raw.wav"
        normalize(source, raw, loudnorm=False)  # original levels: the check hears the real audio
        with wave.open(str(raw), "rb") as handle:
            for label in labels:
                info = record.speakers[label]
                spans, clean_seconds = pick_clips(label, segments, words, clips_per_speaker)
                clips: list[ClipReport] = []
                for number, span in enumerate(spans, start=1):
                    samples = _read_clip(handle, span.start, span.end)
                    name = f"{label}-{number}.wav"
                    _write_clip(target / name, samples)
                    level, snr, clipping = measure(samples.astype("float32") / 32768.0)
                    inside = _span_words(span, words)
                    confidences = [w.confidence for w in inside if w.confidence is not None]
                    confidence = (
                        round(sum(confidences) / len(confidences), 2) if confidences else None
                    )
                    grade, issues = grade_clip(
                        duration=span.duration,
                        level_db=level,
                        snr_db=snr,
                        clipping=clipping,
                        confidence=confidence,
                        overlap=span.overlap,
                    )
                    clips.append(
                        ClipReport(
                            file=name,
                            start=round(span.start, 2),
                            end=round(span.end, 2),
                            text=" ".join(w.text for w in inside),
                            overlap=span.overlap,
                            level_db=level,
                            snr_db=snr,
                            clipping=clipping,
                            confidence=confidence,
                            grade=grade,
                            issues=issues,
                        )
                    )
                reports.append(_speaker_report(info, clips, clean_seconds))
    review = Review(
        meeting=record.id,
        title=record.title,
        date=record.date,
        created_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        names=candidate_names(workspace, record),
        audio=audio_report(segments, words, reports),
        speakers=reports,
    )
    atomic_write_json(target / "review.json", review.model_dump())
    atomic_write_text(target / "answers.toml", answers_template(review))
    atomic_write_text(target / "review.html", review_html(review))
    record.review_status = "pending"
    save_record(folder, record)
    return review


def _speaker_report(
    info: SpeakerInfo, clips: list[ClipReport], clean_seconds: float
) -> SpeakerReport:
    issues: list[str] = []
    order = {"good": 0, "fair": 1, "poor": 2}
    grade: Grade = min((c.grade for c in clips), key=lambda g: order[g], default="poor")
    if not clips:
        issues.append("no clip could be cut: too little recognized speech")
    if info.seconds < 5:
        issues.append(
            f"only {info.seconds:.0f} s of speech: may be part of another speaker's voice "
            "(give both the same name)"
        )
    if clean_seconds < MIN_VOICEPRINT_SECONDS:
        issues.append(f"{clean_seconds:.0f} s without other voices: too little for a voiceprint")
    return SpeakerReport(
        label=info.label,
        suggestion=info.name if info.status == "suggested" else None,
        confidence=info.confidence if info.status == "suggested" else None,
        confirmed=info.name if info.status == "confirmed" else None,
        seconds=info.seconds,
        clean_seconds=clean_seconds,
        grade=grade,
        issues=issues,
        clips=clips,
    )


def default_name(speaker: SpeakerReport) -> str:
    return speaker.confirmed or speaker.suggestion or ""


# --- answers -----------------------------------------------------------------


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # JSON strings are valid TOML basic strings


def answers_template(review: Review) -> str:
    lines = [
        f"# Who is who in {review.title!r} ({review.date}).",
        "# Listen to the clips in this folder (or open review.html), then fill in:",
        "#   name    the person's name; leave it empty if you cannot tell",
        "#   quality how the audio sounds: good | fair | poor",
        "# Apply with: dsr meet identify " + review.meeting + " --answers <this file>",
        "# Voiceprints are stored only for people with recorded consent, never from poor audio.",
        "",
        f"meeting = {_toml_string(review.meeting)}",
        f"audio = {_toml_string(review.audio.grade)}"
        + (f"  # automatic: {'; '.join(review.audio.issues)}" if review.audio.issues else ""),
        'audio_notes = ""',
    ]
    if review.names:
        lines.insert(6, "# Names so far: " + ", ".join(review.names))
    for speaker in review.speakers:
        hint = (
            f"suggested by voice: {speaker.suggestion} ({speaker.confidence:.2f})"
            if speaker.suggestion and speaker.confidence is not None
            else "no suggestion"
        )
        clips = ", ".join(c.file for c in speaker.clips) or "no clips"
        lines += [
            "",
            f"[speakers.{speaker.label}]  # {clock(speaker.seconds)} of speech; {clips}",
            f"name = {_toml_string(default_name(speaker))}  # {hint}",
            f"quality = {_toml_string(speaker.grade)}"
            + (f"  # automatic: {'; '.join(speaker.issues)}" if speaker.issues else ""),
            'notes = ""',
        ]
    return "\n".join(lines) + "\n"


def dump_answers(answers: Answers) -> str:
    lines = [f"meeting = {_toml_string(answers.meeting)}"]
    if answers.audio:
        lines.append(f"audio = {_toml_string(answers.audio)}")
    lines.append(f"audio_notes = {_toml_string(answers.audio_notes)}")
    for label, answer in answers.speakers.items():
        lines += ["", f"[speakers.{label}]", f"name = {_toml_string(answer.name)}"]
        if answer.quality:
            lines.append(f"quality = {_toml_string(answer.quality)}")
        lines.append(f"notes = {_toml_string(answer.notes)}")
    return "\n".join(lines) + "\n"


def load_answers(path: Path) -> Answers:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return Answers.model_validate(data)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise DossierError(f"cannot read answers from {path}: {exc}") from exc


def apply_answers(
    workspace: Workspace, folder: Path, answers: Answers
) -> tuple[MeetingRecord, list[str]]:
    """Confirm the names given, record the audio ratings, store allowed voiceprints."""
    from dossier.meet.pipeline import confirm_speakers, load_record, save_record

    record = load_record(folder)
    if answers.meeting != record.id:
        raise DossierError(f"these answers are for meeting {answers.meeting}, not {record.id}")
    unknown = sorted(set(answers.speakers) - set(record.speakers))
    if unknown:
        raise DossierError(f"unknown speaker label(s) in answers: {', '.join(unknown)}")
    review = load_review(folder)
    clean = {s.label: s.clean_seconds for s in review.speakers} if review else {}
    mapping: dict[str, str] = {}
    enroll: set[str] = set()
    notes: list[str] = []
    for label, answer in answers.speakers.items():
        if answer.quality is not None:
            record.speakers[label] = record.speakers[label].model_copy(
                update={"quality": answer.quality}
            )
        name = answer.name.strip()
        if not name:
            continue
        mapping[label] = name
        if answer.quality == "poor":
            notes.append(f"no voiceprint from {label} ({name}): its audio was rated poor")
        elif label in clean and clean[label] < MIN_VOICEPRINT_SECONDS:
            notes.append(
                f"no voiceprint from {label} ({name}): only {clean[label]:.0f} s without "
                "other voices"
            )
        else:
            enroll.add(label)
    if answers.audio is not None:
        record.audio_quality = answers.audio
    save_record(folder, record)
    if mapping:
        record, more = confirm_speakers(workspace, folder, mapping, enroll=enroll)
        notes += more
    if all(s.status == "confirmed" for s in record.speakers.values()):
        record.review_status = "done"
        removed = clear_clips(folder)
        if removed:
            notes.append(f"every speaker is named; {removed} review clips deleted")
    save_record(folder, record)
    return record, notes


def clear_clips(folder: Path) -> int:
    """Delete the clip audio (voice recordings); the reports and answers stay."""
    target = review_dir(folder)
    clips = list(target.glob("*.wav")) if target.is_dir() else []
    for clip in clips:
        clip.unlink()
    return len(clips)


def remove_review(folder: Path) -> None:
    shutil.rmtree(review_dir(folder), ignore_errors=True)


# --- the review page ---------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Who is speaking? {title}</title>
<style>
:root {{ color-scheme: light dark; --muted: #6b7280; --line: #d1d5db;
  --good: #15803d; --fair: #b45309; --poor: #b91c1c; }}
body {{ font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  max-width: 52rem; margin: 2rem auto; padding: 0 1rem; }}
h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; }}
h2 {{ font-size: 1.1rem; margin: 0 0 .25rem; }}
.muted {{ color: var(--muted); }}
section {{ border: 1px solid var(--line); border-radius: .5rem; padding: 1rem 1.25rem;
  margin: 1rem 0; }}
.clip {{ margin: .75rem 0; }}
.clip audio {{ width: 100%; }}
.clip p {{ margin: .2rem 0; }}
.badge {{ font-size: .75rem; font-weight: 600; padding: 0 .5rem; border-radius: 1rem;
  border: 1px solid currentColor; white-space: nowrap; }}
.good {{ color: var(--good); }} .fair {{ color: var(--fair); }} .poor {{ color: var(--poor); }}
ul {{ margin: .25rem 0; padding-left: 1.25rem; }}
.fields {{ display: grid; grid-template-columns: 2fr 1fr; gap: .75rem; margin-top: .75rem; }}
label {{ display: block; font-weight: 600; }}
input, select, textarea, button {{ font: inherit; }}
input, select {{ width: 100%; box-sizing: border-box; padding: .3rem .5rem; }}
textarea {{ width: 100%; min-height: 12rem; box-sizing: border-box;
  font-family: ui-monospace, monospace; font-size: .85rem; }}
button {{ padding: .5rem 1.25rem; }}
</style>
</head>
<body data-meeting="{meeting}">
<h1>Who is speaking?</h1>
<p class="muted">{title} · {date} · {count} speaker(s) to identify</p>
<p>Listen to each speaker's clips, write who it is (leave it empty if you cannot tell),
and say how the audio sounds. Then press <b>Save answers</b> and apply the file with
<code>dsr meet identify {meeting} --answers &lt;file&gt;</code>.</p>
<section id="audio">
<h2>The recording <span class="badge {audio_grade}">automatic check: {audio_grade}</span></h2>
{audio_issues}
<div class="fields">
<label>Notes on the audio <input name="notes" placeholder="optional"></label>
<label>Overall audio {audio_select}</label>
</div>
</section>
{speakers}
<datalist id="names">{names}</datalist>
<p><button id="save" type="button">Save answers</button></p>
<textarea id="out" readonly hidden aria-label="Answers"></textarea>
<script>
const str = (value) => JSON.stringify(value);
document.getElementById("save").addEventListener("click", () => {{
  const meeting = document.body.dataset.meeting;
  const audio = document.getElementById("audio");
  const lines = [
    `meeting = ${{str(meeting)}}`,
    `audio = ${{str(audio.querySelector("[name=quality]").value)}}`,
    `audio_notes = ${{str(audio.querySelector("[name=notes]").value.trim())}}`,
  ];
  for (const section of document.querySelectorAll("section[data-label]")) {{
    lines.push("", `[speakers.${{section.dataset.label}}]`);
    for (const field of ["name", "quality", "notes"]) {{
      lines.push(`${{field}} = ${{str(section.querySelector(`[name=${{field}}]`).value.trim())}}`);
    }}
  }}
  const text = lines.join("\\n") + "\\n";
  const out = document.getElementById("out");
  out.value = text;
  out.hidden = false;
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([text], {{ type: "text/plain" }}));
  link.download = `answers-${{meeting}}.toml`;
  link.click();
}});
</script>
</body>
</html>
"""


def _select(selected: str) -> str:
    options = "".join(
        f'<option value="{g}"{" selected" if g == selected else ""}>{g}</option>' for g in _GRADES
    )
    return f'<select name="quality">{options}</select>'


def _issues(issues: list[str]) -> str:
    if not issues:
        return ""
    return "<ul>" + "".join(f"<li>{html.escape(i)}</li>" for i in issues) + "</ul>"


def review_html(review: Review) -> str:
    sections = []
    for speaker in review.speakers:
        e = html.escape
        hint = ""
        if speaker.suggestion and speaker.confidence is not None:
            hint = (
                f" · suggested by voice: <b>{e(speaker.suggestion)}</b> ({speaker.confidence:.2f})"
            )
        elif speaker.confirmed:
            hint = f" · confirmed as <b>{e(speaker.confirmed)}</b>"
        clips = []
        for clip in speaker.clips:
            clip_issues = f" · {e('; '.join(clip.issues))}" if clip.issues else ""
            clips.append(
                '<div class="clip">'
                f'<p class="muted">[{clock(clip.start)}] · {clip.end - clip.start:.0f} s · '
                f'<span class="badge {clip.grade}">{clip.grade}</span>{clip_issues}</p>'
                f'<audio controls preload="none" src="{e(clip.file)}"></audio>'
                f"<p>“{e(clip.text)}”</p></div>"
            )
        sections.append(
            f'<section data-label="{e(speaker.label)}">'
            f"<h2>{e(speaker.label)} "
            f'<span class="badge {speaker.grade}">audio {speaker.grade}</span></h2>'
            f'<p class="muted">{clock(speaker.seconds)} of speech · '
            f"{clock(speaker.clean_seconds)} without other voices{hint}</p>"
            + _issues(speaker.issues)
            + "".join(clips)
            + '<div class="fields">'
            f'<label>Name <input name="name" list="names" value="{e(default_name(speaker))}" '
            'placeholder="who is this?"></label>'
            f"<label>Audio {_select(speaker.grade)}</label>"
            "</div>"
            '<label>Notes <input name="notes" placeholder="optional"></label>'
            "</section>"
        )
    return _PAGE.format(
        title=html.escape(review.title),
        date=html.escape(review.date),
        meeting=html.escape(review.meeting),
        count=len(review.speakers),
        audio_grade=review.audio.grade,
        audio_issues=_issues(review.audio.issues),
        audio_select=_select(review.audio.grade),
        speakers="\n".join(sections),
        names="".join(f'<option value="{html.escape(n)}">' for n in review.names),
    )
