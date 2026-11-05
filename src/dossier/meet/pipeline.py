"""Process one recording into ``meetings/<date>-<slug>-<hash>/``.

Stages (each recorded in ``meeting.json`` with a version, so a re-run resumes
and a changed stage re-runs it and everything after it):

    audio -> asr -> diarize -> assign -> identify -> deck -> summarize -> render -> review

``review`` (with ``[meet] speaker_review``) cuts short clips of unidentified
speakers for a person to name; see :mod:`dossier.meet.review`. It comes last
because nothing else depends on it.

A meeting is identified by the content hash of its recording: the same file
added twice (or moved) is the same meeting.

Language: each meeting has one spoken language, set explicitly or detected
(``auto``) at the start of the ``asr`` stage. Glossary replacements are applied
in ``assign``, so ``machine/words.json`` keeps what the recognizer heard and a
changed glossary only needs ``--force assign``.
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
from dossier.core.config import WorkspaceConfig
from dossier.core.errors import DossierError, ProviderUnavailableError
from dossier.core.io import atomic_write_json, atomic_write_text, read_json, sha256_file
from dossier.core.languages import AUTO, LANGUAGES, language_code, language_name
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
    detect_language,
)
from dossier.meet.glossary import Glossary
from dossier.meet.models import (
    DiarSegment,
    MeetingRecord,
    StageRecord,
    Turn,
    Word,
    language_label,
)
from dossier.meet.render import write_outputs
from dossier.meet.speakers import assign, speaker_table, suggest_names, to_turns
from dossier.meet.summarize import Item, MeetingSummary, summarize

log = logging.getLogger("dossier.meet")

STAGES: dict[str, int] = {
    "audio": 1,
    "asr": 1,
    "diarize": 1,
    "assign": 2,  # 2: glossary replacements applied here, across words
    "identify": 1,
    "deck": 1,
    "summarize": 1,
    "render": 1,
    "review": 1,
}
Progress = Callable[[str, str], None]


@dataclass
class MeetOptions:
    title: str | None = None
    roster: list[str] = field(default_factory=list)
    deck: Path | None = None
    language: str | None = None  # a code or "auto"; default: default_language()
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


def workspace_languages(config: WorkspaceConfig) -> list[str]:
    """The workspace's languages as Whisper codes (unknown entries are skipped)."""
    codes: list[str] = []
    for value in config.workspace.languages:
        code = language_code(value)
        if code in LANGUAGES:
            codes.append(code)
        else:
            log.warning("ignoring unknown workspace language %r for meetings", value)
    return list(dict.fromkeys(codes))


def default_language(config: WorkspaceConfig) -> str:
    """``[meet] language``; else auto for a multilingual workspace; else its language."""
    if config.meet.language:
        return config.meet.language
    languages = workspace_languages(config)
    return languages[0] if len(languages) == 1 else AUTO


def _set_language(record: MeetingRecord, requested: str) -> bool:
    """Apply a requested language to an existing meeting; True if ASR must re-run."""
    if requested == AUTO:
        if record.language_source == "detected":
            return False  # already detected; detecting again gives the same answer
        record.language, record.language_confidence = AUTO, None
        return True
    if requested == record.language:
        record.language_source, record.language_confidence = "set", None
        return False
    record.language, record.language_source, record.language_confidence = requested, "set", None
    return True


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
    force = set(options.force)
    requested = None
    if options.language is not None:
        requested = language_code(options.language)
        if requested is None:
            raise DossierError(
                f'unknown language {options.language!r}; use a code such as "es", or "auto"'
            )
    sha = sha256_file(source)
    folder = find_meeting(workspace, sha)
    if folder is not None:
        record = load_record(folder)
        if requested is not None and _set_language(record, requested):
            force.add("asr")
    else:
        date = recorded_date(source)
        title = options.title or _title(source.stem)
        record = MeetingRecord(
            id=f"{date}-{_slug(title)}-{sha[:6]}",
            title=title,
            date=date,
            source=_relative(workspace, source),
            sha256=sha,
            language=requested or default_language(workspace.config),
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
    glossaries = Glossary.load(
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
            and stage not in force
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
            if record.language == AUTO:
                _detect(workspace, engines.asr, wav, record)
            words = engines.asr.transcribe(
                wav,
                language=record.language,
                prompt=glossaries.for_language(record.language).prompt(record.language),
            )
            atomic_write_json(machine / "words.json", [w.model_dump() for w in words])
            record.asr = f"{engines.asr.name}:{engines.asr.model_id}"
            finish("asr", detail=f"{len(words)} words, {language_label(record)}")
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
            words = glossaries.for_language(record.language).apply_words(words)
            turns = to_turns(assign(words, diar.segments))
            atomic_write_json(machine / "turns.json", [t.model_dump() for t in turns])
            table = speaker_table(diar.segments, turns)
            for label, info in record.speakers.items():  # keep confirmed names and ratings
                if label in table and info.status == "confirmed":
                    table[label] = table[label].model_copy(
                        update={"name": info.name, "status": "confirmed", "quality": info.quality}
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
                language=record.language,
            )
            atomic_write_json(machine / "summary.json", _summary_to_json(summary))
            record.summary_status = "ok" if summary.status == "ok" else "degraded"
            finish("summarize", detail=f"{summary.calls} calls, {summary.dropped_items} dropped")
        # Rendering is cheap and must reflect speaker confirmations, so it always runs.
        needed("render")
        write_outputs(folder, record, turns, summary_from_json(read_json(machine / "summary.json")))
        finish("render")
        if needed("review"):
            _review_stage(workspace, folder, record, source, finish)
    if wav.exists() and not settings.keep_audio:
        wav.unlink()
    return record


def _review_stage(
    workspace: Workspace,
    folder: Path,
    record: MeetingRecord,
    source: Path,
    finish: Callable[..., None],
) -> None:
    if not workspace.config.meet.speaker_review:
        finish("review", "skipped", "speaker_review is off")
        return
    if all(info.status == "confirmed" for info in record.speakers.values()):
        finish("review", "skipped", "every speaker is confirmed")
        return
    from dossier.meet.review import build_review

    try:
        review = build_review(workspace, folder, recording=source)
    except DossierError as exc:  # the meeting itself is fine; only the clips are missing
        log.warning("meeting %s: speaker review not prepared: %s", record.id, exc.code)
        finish("review", "failed", exc.code)
        return
    record.review_status = "pending"
    clips = sum(len(s.clips) for s in review.speakers) if review else 0
    finish("review", detail=f"{len(review.speakers) if review else 0} speakers, {clips} clips")


def _detect(workspace: Workspace, asr: AsrBackend, wav: Path, record: MeetingRecord) -> None:
    languages = workspace_languages(workspace.config)
    candidates = languages if len(languages) > 1 else None
    guess = detect_language(asr, wav, candidates)
    record.language = guess.language
    record.language_source = "detected"
    record.language_confidence = guess.probability
    if candidates and guess.heard not in candidates and guess.heard_probability >= 0.5:
        log.warning(
            "meeting %s sounds like %s, which is not among the workspace languages; "
            "transcribing as %s (use --language to change it)",
            record.id,
            language_name(guess.heard),
            language_name(guess.language),
        )
    elif guess.probability < 0.7:
        log.warning(
            "meeting %s: language detection is unsure (%s, %.2f); check it with "
            "`dsr meet list` and use --language to change it",
            record.id,
            language_name(guess.language),
            guess.probability,
        )


def rerender(workspace: Workspace, folder: Path) -> MeetingRecord:
    record = load_record(folder)
    machine = folder / "machine"
    turns = [Turn(**t) for t in read_json(machine / "turns.json")]
    summary = summary_from_json(read_json(machine / "summary.json"))
    write_outputs(folder, record, turns, summary)
    return record


def confirm_speakers(
    workspace: Workspace,
    folder: Path,
    mapping: dict[str, str],
    *,
    enroll: set[str] | None = None,
) -> tuple[MeetingRecord, list[str]]:
    """Confirm names for speaker labels; enroll voiceprints where consent exists
    (only for the labels in ``enroll``, when given)."""
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
        allowed = enroll is None or label in enroll
        if settings.speaker_voiceprints and embedding and allowed:
            try:
                people_store.enroll(workspace.paths, name, embedding, settings.diarization_model)
                notes.append(f"voiceprint updated for {name}")
            except DossierError as exc:
                notes.append(exc.message)
    if record.review_status == "pending" and all(
        info.status == "confirmed" for info in record.speakers.values()
    ):
        from dossier.meet.review import clear_clips

        record.review_status = "done"
        removed = clear_clips(folder)
        if removed:
            notes.append(f"every speaker is named; {removed} review clips deleted")
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
        language=record.language,
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
