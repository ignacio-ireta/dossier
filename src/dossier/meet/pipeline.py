"""Process one recording into ``meetings/<date>-<slug>-<hash>/``.

Stages (each recorded in ``meeting.json`` with a version, so a re-run resumes
and a changed stage re-runs it and everything after it):

    audio -> asr -> diarize -> assign -> identify -> deck -> summarize -> render

A meeting is identified by the content hash of its recording: the same file
added twice (or moved) is the same meeting.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dossier import platform
from dossier.core.errors import DossierError, ProviderUnavailableError
from dossier.core.io import atomic_write_json, atomic_write_text, read_json, sha256_file
from dossier.core.workspace import Workspace
from dossier.llm.router import ModelRouter, RoleClient
from dossier.meet import people as people_store
from dossier.meet.audio import normalize, recorded_date
from dossier.meet.backends import (
    AsrBackend,
    Diarization,
    Diarizer,
    PyannoteDiarizer,
    SingleSpeaker,
    asr_backend,
)
from dossier.meet.glossary import Glossary
from dossier.meet.models import (
    DiarSegment,
    MeetingRecord,
    StageRecord,
    Turn,
    Word,
)
from dossier.meet.render import write_outputs
from dossier.meet.speakers import assign, speaker_table, suggest_names, to_turns
from dossier.meet.summarize import Item, MeetingSummary, summarize

log = logging.getLogger("dossier.meet")

STAGES: dict[str, int] = {
    "audio": 1,
    "asr": 1,
    "diarize": 1,
    "assign": 1,
    "identify": 1,
    "deck": 1,
    "summarize": 1,
    "render": 1,
}
Progress = Callable[[str, str], None]


@dataclass
class MeetOptions:
    title: str | None = None
    roster: list[str] = field(default_factory=list)
    deck: Path | None = None
    diarize: bool = True
    force: set[str] = field(default_factory=set)
    scheduled: bool = False


@dataclass
class Engines:
    asr: AsrBackend
    diarizer: Diarizer


def default_engines(workspace: Workspace, *, diarize: bool = True) -> Engines:
    settings = workspace.config.meet
    diarizer: Diarizer = (
        PyannoteDiarizer(settings.diarization_model) if diarize else SingleSpeaker()
    )
    return Engines(asr=asr_backend(settings), diarizer=diarizer)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "meeting"


def _title(stem: str) -> str:
    text = re.sub(r"[_\-.]+", " ", stem)
    text = re.sub(r"\b20\d{2}\s?\d{2}\s?\d{2}\b", "", text).strip()
    return text[:1].upper() + text[1:] if text else "Meeting"


def _registry_path(workspace: Workspace) -> Path:
    return workspace.paths.state / "state" / "meet" / "by_hash.json"


def find_meeting(workspace: Workspace, sha256: str) -> Path | None:
    path = _registry_path(workspace)
    if path.is_file():
        known: dict[str, str] = read_json(path)
        meeting_id = known.get(sha256)
        if meeting_id and (workspace.paths.meetings / meeting_id / "meeting.json").is_file():
            return workspace.paths.meetings / meeting_id
    return None


def _register(workspace: Workspace, sha256: str, meeting_id: str) -> None:
    path = _registry_path(workspace)
    data = read_json(path) if path.is_file() else {}
    data[sha256] = meeting_id
    atomic_write_json(path, data)


def load_record(folder: Path) -> MeetingRecord:
    return MeetingRecord.model_validate_json((folder / "meeting.json").read_text(encoding="utf-8"))


def save_record(folder: Path, record: MeetingRecord) -> None:
    atomic_write_text(folder / "meeting.json", record.model_dump_json(indent=1) + "\n")


def resolve_meeting(workspace: Workspace, reference: str) -> Path:
    ref = reference.strip().strip("/").removeprefix("meetings/").split("/")[0]
    folder = workspace.paths.meetings / ref
    if (folder / "meeting.json").is_file():
        return folder
    matches = [
        p for p in workspace.paths.meetings.glob(f"*{ref}*") if (p / "meeting.json").is_file()
    ]
    if len(matches) == 1:
        return matches[0]
    raise DossierError(
        f"no meeting matches {reference!r}"
        if not matches
        else f"{reference!r} matches {len(matches)} meetings; be more specific"
    )


def _summary_to_json(summary: MeetingSummary) -> dict[str, object]:
    return asdict(summary)


def summary_from_json(data: dict[str, Any]) -> MeetingSummary:
    def items(key: str) -> list[Item]:
        return [Item(**item) for item in data.get(key, [])]

    return MeetingSummary(
        overview=str(data.get("overview", "")),
        topics=list(data.get("topics", [])),
        decisions=items("decisions"),
        actions=items("actions"),
        questions=items("questions"),
        status=str(data.get("status", "ok")),
        windows=int(data.get("windows", 0)),
        degraded_windows=int(data.get("degraded_windows", 0)),
        dropped_items=int(data.get("dropped_items", 0)),
        calls=int(data.get("calls", 0)),
    )


def _meet_client(workspace: Workspace, scheduled: bool) -> RoleClient | None:
    router = ModelRouter(workspace.config, scheduled=scheduled)
    if not router.has("meet"):
        return None
    client = router.client("meet")
    try:
        client.provider.list_models()
    except ProviderUnavailableError:
        return None
    return client


def _relative(workspace: Workspace, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(workspace.root.resolve()))
    except ValueError:
        return str(path.resolve())


def process_recording(
    workspace: Workspace,
    source: Path,
    options: MeetOptions | None = None,
    *,
    engines: Engines | None = None,
    progress: Progress | None = None,
) -> MeetingRecord:
    options = options or MeetOptions()
    if not source.is_file():
        raise DossierError(f"{source} not found")
    settings = workspace.config.meet
    sha = sha256_file(source)
    folder = find_meeting(workspace, sha)
    if folder is not None:
        record = load_record(folder)
    else:
        date = recorded_date(source)
        title = options.title or _title(source.stem)
        record = MeetingRecord(
            id=f"{date}-{_slug(title)}-{sha[:6]}",
            title=title,
            date=date,
            source=_relative(workspace, source),
            sha256=sha,
            language=settings.language or workspace.config.workspace.languages[0],
        )
        folder = workspace.paths.meetings / record.id
    if options.title:
        record.title = options.title
    if options.roster:
        record.roster = list(options.roster)
    machine = folder / "machine"
    machine.mkdir(parents=True, exist_ok=True)
    save_record(folder, record)
    _register(workspace, sha, record.id)
    engines = engines or default_engines(workspace, diarize=options.diarize)
    glossary = Glossary.load(
        workspace.paths.resolve(settings.glossary) if settings.glossary else None
    )
    wav = machine / "audio.wav"
    rerun = False

    def needed(stage: str) -> bool:
        nonlocal rerun
        previous = record.stages.get(stage)
        fresh = (
            previous is not None
            and previous.status in {"done", "skipped"}
            and previous.version == STAGES[stage]
            and stage not in options.force
        )
        if not fresh:
            rerun = True
        return rerun

    def finish(stage: str, status: str = "done", detail: str = "") -> None:
        record.stages[stage] = StageRecord(
            status=status,
            version=STAGES[stage],
            finished_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
            detail=detail,
        )
        save_record(folder, record)
        if progress:
            progress(stage, record.id)

    with platform.current().keep_awake.hold("dsr meet"):
        audio_needed = needed("audio")
        asr_needed = needed("asr")
        diarize_needed = needed("diarize")
        if audio_needed or ((asr_needed or diarize_needed) and not wav.exists()):
            record.duration_s = round(normalize(source, wav), 2)
            finish("audio")
        if asr_needed:
            words = engines.asr.transcribe(wav, language=record.language, prompt=glossary.prompt())
            words = [w.model_copy(update={"text": glossary.apply(w.text)}) for w in words]
            atomic_write_json(machine / "words.json", [w.model_dump() for w in words])
            record.asr = f"{engines.asr.name}:{engines.asr.model_id}"
            finish("asr", detail=f"{len(words)} words")
        if diarize_needed:
            result = engines.diarizer.diarize(wav)
            atomic_write_json(
                machine / "diarization.json",
                {
                    "segments": [s.model_dump() for s in result.segments],
                    "embeddings": result.embeddings,
                },
            )
            record.diarization = engines.diarizer.name
            finish("diarize", detail=f"{len({s.speaker for s in result.segments})} speakers")
        words = [Word(**w) for w in read_json(machine / "words.json")]
        diar_data = read_json(machine / "diarization.json")
        diar = Diarization(
            segments=[DiarSegment(**s) for s in diar_data["segments"]],
            embeddings=diar_data.get("embeddings", {}),
        )
        if needed("assign"):
            turns = to_turns(assign(words, diar.segments))
            atomic_write_json(machine / "turns.json", [t.model_dump() for t in turns])
            table = speaker_table(diar.segments, turns)
            for label, info in record.speakers.items():  # keep confirmed names
                if label in table and info.status == "confirmed":
                    table[label] = table[label].model_copy(
                        update={"name": info.name, "status": "confirmed"}
                    )
            record.speakers = table
            finish("assign")
        turns = [Turn(**t) for t in read_json(machine / "turns.json")]
        if needed("identify"):
            if settings.speaker_voiceprints and diar.embeddings:
                prints = people_store.voiceprints(workspace.paths, settings.diarization_model)
                record.speakers = suggest_names(
                    record.speakers,
                    diar.embeddings,
                    prints,
                    threshold=settings.voiceprint_threshold,
                    roster=record.roster or None,
                )
                finish("identify", detail=f"{len(prints)} voiceprints")
            else:
                finish("identify", "skipped", "voiceprints off or no embeddings")
        context = ""
        if needed("deck"):
            if options.deck is not None:
                from dossier.meet.deck import deck_markdown

                atomic_write_text(folder / "deck.md", deck_markdown(options.deck))
                record.deck = _relative(workspace, options.deck)
                finish("deck")
            else:
                finish("deck", "skipped")
        if (folder / "deck.md").is_file():
            from dossier.index.chunking import outline

            context = "\n".join(
                e.text
                for e in outline((folder / "deck.md").read_text(encoding="utf-8"))
                if e.level == 2
            )
        if needed("summarize"):
            client = _meet_client(workspace, options.scheduled)
            summary = summarize(
                client,
                workspace.config,
                title=record.title,
                turns=turns,
                speakers=record.speakers,
                context=context,
            )
            atomic_write_json(machine / "summary.json", _summary_to_json(summary))
            record.summary_status = "ok" if summary.status == "ok" else "degraded"
            finish("summarize", detail=f"{summary.calls} calls, {summary.dropped_items} dropped")
        # Rendering is cheap and must reflect speaker confirmations, so it always runs.
        needed("render")
        write_outputs(folder, record, turns, summary_from_json(read_json(machine / "summary.json")))
        finish("render")
    if wav.exists() and not settings.keep_audio:
        wav.unlink()
    return record


def rerender(workspace: Workspace, folder: Path) -> MeetingRecord:
    record = load_record(folder)
    machine = folder / "machine"
    turns = [Turn(**t) for t in read_json(machine / "turns.json")]
    summary = summary_from_json(read_json(machine / "summary.json"))
    write_outputs(folder, record, turns, summary)
    return record


def confirm_speakers(
    workspace: Workspace, folder: Path, mapping: dict[str, str]
) -> tuple[MeetingRecord, list[str]]:
    """Confirm names for speaker labels; enroll voiceprints where consent exists."""
    record = load_record(folder)
    notes: list[str] = []
    unknown = sorted(set(mapping) - set(record.speakers))
    if unknown:
        raise DossierError(
            f"unknown speaker label(s): {', '.join(unknown)}; known: {', '.join(record.speakers)}"
        )
    diar = read_json(folder / "machine" / "diarization.json")
    settings = workspace.config.meet
    for label, name in mapping.items():
        record.speakers[label] = record.speakers[label].model_copy(
            update={"name": name, "status": "confirmed", "confidence": None}
        )
        embedding = diar.get("embeddings", {}).get(label)
        if settings.speaker_voiceprints and embedding:
            try:
                people_store.enroll(workspace.paths, name, embedding, settings.diarization_model)
                notes.append(f"voiceprint updated for {name}")
            except DossierError as exc:
                notes.append(exc.message)
    save_record(folder, record)
    rerender(workspace, folder)
    return record, notes


def resummarize(workspace: Workspace, folder: Path) -> MeetingRecord:
    record = load_record(folder)
    machine = folder / "machine"
    turns = [Turn(**t) for t in read_json(machine / "turns.json")]
    client = _meet_client(workspace, scheduled=False)
    if client is None:
        raise DossierError("the meet model is not reachable; start the model server first")
    context = ""
    if (folder / "deck.md").is_file():
        context = (folder / "deck.md").read_text(encoding="utf-8")[:4000]
    summary = summarize(
        client,
        workspace.config,
        title=record.title,
        turns=turns,
        speakers=record.speakers,
        context=context,
    )
    atomic_write_json(machine / "summary.json", _summary_to_json(summary))
    record.summary_status = "ok" if summary.status == "ok" else "degraded"
    save_record(folder, record)
    write_outputs(folder, record, turns, summary)
    return record


def list_meetings(workspace: Workspace) -> list[MeetingRecord]:
    records = []
    for path in sorted(workspace.paths.meetings.glob("*/meeting.json")):
        try:
            records.append(load_record(path.parent))
        except (OSError, ValueError):
            log.warning("unreadable meeting record %s", path.parent.name)
    return sorted(records, key=lambda r: (r.date, r.id), reverse=True)


def speakers_summary(record: MeetingRecord) -> list[dict[str, object]]:
    return [
        {"label": s.label, "display": s.display(), "status": s.status, "seconds": s.seconds}
        for s in record.speakers.values()
    ]


def meeting_json(record: MeetingRecord) -> dict[str, object]:
    data: dict[str, object] = json.loads(record.model_dump_json())
    data["speakers"] = speakers_summary(record)
    return data
