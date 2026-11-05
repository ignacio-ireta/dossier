"""Write transcript.md, summary.md, actions.json and subtitle files."""

from __future__ import annotations

import json
from pathlib import Path

from dossier.core.io import atomic_write_json, atomic_write_text
from dossier.meet.models import MeetingRecord, SpeakerInfo, Turn, clock
from dossier.meet.summarize import MeetingSummary

NOTE_NAMES = (
    "Speaker names marked *likely* are suggestions from voice similarity and are not "
    "confirmed; confirm with `dsr meet speakers`."
)


def _front(record: MeetingRecord, kind: str, extra: dict[str, object] | None = None) -> str:
    fields: dict[str, object] = {
        "meeting": record.id,
        "kind": kind,
        "title": record.title,
        "date": record.date,
        "duration": clock(record.duration_s or 0),
        "language": record.language,
        "speakers": len(record.speakers),
        **(extra or {}),
    }
    lines = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fields.items()]
    return "\n".join([*lines, "---"])


def _speaker_lines(speakers: dict[str, SpeakerInfo]) -> list[str]:
    return [f"- {info.display()}: {clock(info.seconds)} of speech" for info in speakers.values()]


def merge_turns(turns: list[Turn]) -> list[Turn]:
    """Merge consecutive turns by the same speaker for reading."""
    merged: list[Turn] = []
    for turn in turns:
        if merged and merged[-1].speaker == turn.speaker and turn.start - merged[-1].end < 3:
            last = merged[-1]
            merged[-1] = last.model_copy(
                update={"end": turn.end, "text": f"{last.text} {turn.text}"}
            )
        else:
            merged.append(turn)
    return merged


def transcript_md(record: MeetingRecord, turns: list[Turn]) -> str:
    parts = [
        _front(record, "transcript"),
        "",
        f"# {record.title}",
        "",
        f"{record.date} · {clock(record.duration_s or 0)} · {len(record.speakers)} speaker(s)",
        "",
        "## Speakers",
        "",
        *_speaker_lines(record.speakers),
        "",
    ]
    if any(info.status == "suggested" for info in record.speakers.values()):
        parts += [f"> {NOTE_NAMES}", ""]
    parts += ["## Transcript", ""]
    for turn in merge_turns(turns):
        info = record.speakers.get(turn.speaker)
        who = info.display() if info else turn.speaker
        parts += [f"**[{clock(turn.start)}] {who}:** {turn.text}", ""]
    return "\n".join(parts).rstrip() + "\n"


def _srt_time(seconds: float, sep: str) -> str:
    millis = round(seconds * 1000)
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{millis:03d}"


def subtitles(record: MeetingRecord, turns: list[Turn]) -> tuple[str, str]:
    srt: list[str] = []
    vtt = ["WEBVTT", ""]
    for number, turn in enumerate(turns, start=1):
        info = record.speakers.get(turn.speaker)
        who = info.display() if info else turn.speaker
        srt += [
            str(number),
            f"{_srt_time(turn.start, ',')} --> {_srt_time(turn.end, ',')}",
            f"{who}: {turn.text}",
            "",
        ]
        vtt += [
            f"{_srt_time(turn.start, '.')} --> {_srt_time(turn.end, '.')}",
            f"<v {who}>{turn.text}",
            "",
        ]
    return "\n".join(srt), "\n".join(vtt)


def confirmed_names(record: MeetingRecord, text: str) -> str:
    """Replace speaker labels with confirmed names (never with suggestions)."""
    for label, info in record.speakers.items():
        if info.status == "confirmed" and info.name:
            text = text.replace(label, info.name)
    return text


def summary_md(record: MeetingRecord, summary: MeetingSummary) -> str:
    def names(text: str) -> str:
        return confirmed_names(record, text)

    parts = [
        _front(record, "summary", {"status": summary.status}),
        "",
        f"# Summary: {record.title}",
        "",
        f"{record.date} · {clock(record.duration_s or 0)} · transcript: `transcript.md`",
        "",
        "## Overview",
        "",
        names(summary.overview),
        "",
    ]
    if summary.topics:
        parts += ["## Topics", "", ", ".join(summary.topics), ""]
    parts += ["## Decisions", ""]
    parts += [f"- {names(d.text)} [{d.stamp()}]" for d in summary.decisions] or ["- None stated."]
    parts += ["", "## Action items", ""]
    if summary.actions:
        parts += ["| Task | Owner | Due | At |", "| --- | --- | --- | --- |"]
        parts += [
            f"| {names(a.text).replace('|', '/')} | {names(a.owner)} | {a.due} | [{a.stamp()}] |"
            for a in summary.actions
        ]
    else:
        parts.append("- None stated.")
    parts += ["", "## Open questions", ""]
    parts += [f"- {names(q.text)} [{q.stamp()}]" for q in summary.questions] or ["- None stated."]
    parts += [
        "",
        "## Notes",
        "",
        "- Every item cites the time it is said; times were checked against the transcript.",
        "- Owners and due dates appear only when stated in the meeting; otherwise UNKNOWN.",
    ]
    if summary.status == "degraded":
        parts.append(
            f"- Incomplete: {summary.degraded_windows} of {summary.windows} parts could not be "
            "summarized; re-run `dsr meet summarize` when the model is available."
        )
    if any(info.status == "suggested" for info in record.speakers.values()):
        parts.append(f"- {NOTE_NAMES}")
    return "\n".join(parts).rstrip() + "\n"


def write_outputs(
    folder: Path, record: MeetingRecord, turns: list[Turn], summary: MeetingSummary | None
) -> None:
    atomic_write_text(folder / "transcript.md", transcript_md(record, turns))
    srt, vtt = subtitles(record, turns)
    atomic_write_text(folder / "machine" / "transcript.srt", srt)
    atomic_write_text(folder / "machine" / "transcript.vtt", vtt)
    if summary is not None:
        atomic_write_text(folder / "summary.md", summary_md(record, summary))
        atomic_write_json(
            folder / "actions.json",
            [
                {
                    "task": a.text,
                    "owner": a.owner,
                    "due": a.due,
                    "at": a.stamp(),
                    "at_seconds": round(a.at, 1),
                }
                for a in summary.actions
            ],
        )
