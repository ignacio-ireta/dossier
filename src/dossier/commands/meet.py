"""Meeting commands: ``dsr meet ...`` and ``dsr people ...``."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.table import Table

from dossier.commands.common import State, console, emit, get_state
from dossier.core.errors import DossierError
from dossier.core.lock import WorkspaceLock
from dossier.core.policy import require_not_prohibited, require_processing
from dossier.meet.models import Grade, MeetingRecord, clock

if TYPE_CHECKING:
    from dossier.meet.review import Answers, Review, SpeakerReport

meet_app = typer.Typer(help="Transcribe and summarize meetings.", no_args_is_help=True)
people_app = typer.Typer(help="Opt-in voiceprints for speaker suggestions.", no_args_is_help=True)


def register(app: typer.Typer) -> None:
    app.add_typer(meet_app, name="meet")
    app.add_typer(people_app, name="people")

    @meet_app.command("add")
    def meet_add(
        ctx: typer.Context,
        recording: Annotated[Path, typer.Argument(help="Audio or video file.")],
        title: Annotated[str | None, typer.Option(help="Meeting title.")] = None,
        attendee: Annotated[
            list[str] | None, typer.Option(help="Attendee name (limits speaker suggestions).")
        ] = None,
        deck: Annotated[Path | None, typer.Option(help="Slides shown (.pptx or .pdf).")] = None,
        language: Annotated[
            str | None,
            typer.Option(
                help='Spoken language: a code such as "es" or "en", or "auto" to detect it. '
                "Default: the workspace's [meet] language. Changing it re-transcribes."
            ),
        ] = None,
        diarize: Annotated[bool, typer.Option(help="Split by speaker.")] = True,
        force: Annotated[
            list[str] | None, typer.Option(help="Re-run a stage (and those after it).")
        ] = None,
    ) -> None:
        """Transcribe, split by speaker, and summarize one recording."""
        from dossier.core.languages import language_code
        from dossier.meet.models import language_label
        from dossier.meet.pipeline import MeetOptions, meeting_json, process_recording

        if language is not None and language_code(language) is None:
            raise typer.BadParameter(
                f'unknown language {language!r}; use a code such as "es" or "en", or "auto"',
                param_hint="--language",
            )
        state = get_state(ctx)
        workspace = state.workspace()
        require_processing(workspace.config, "meeting transcription")
        options = MeetOptions(
            title=title,
            roster=attendee or [],
            deck=deck,
            language=language,
            diarize=diarize,
            force=set(force or []),
        )

        def progress(stage: str, meeting_id: str) -> None:
            if not state.json:
                console.print(f"  {stage} done", highlight=False)

        with WorkspaceLock(workspace.paths.locks, "meet"):
            record = process_recording(workspace, recording.resolve(), options, progress=progress)
        if state.json:
            emit(state, meeting_json(record))
        else:
            console.print(f"[bold]{record.id}[/bold]: meetings/{record.id}/summary.md")
            console.print(f"  language: {language_label(record)}", highlight=False)

    @meet_app.command("list")
    def meet_list(ctx: typer.Context) -> None:
        """Meetings in this workspace, newest first."""
        from dossier.meet.models import language_label
        from dossier.meet.pipeline import list_meetings, speakers_summary

        state = get_state(ctx)
        workspace = state.workspace()
        require_not_prohibited(workspace.config, "meetings")
        records = list_meetings(workspace)
        if state.json:
            emit(
                state,
                [
                    {
                        "id": r.id,
                        "title": r.title,
                        "date": r.date,
                        "duration": clock(r.duration_s or 0),
                        "language": r.language,
                        "language_source": r.language_source,
                        "language_confidence": r.language_confidence,
                        "speakers": speakers_summary(r),
                        "to_identify": _unnamed(r),
                        "summary_status": r.summary_status,
                    }
                    for r in records
                ],
            )
            return
        table = Table("id", "title", "date", "duration", "language", "speakers", "summary")
        for r in records:
            table.add_row(
                r.id,
                r.title,
                r.date,
                clock(r.duration_s or 0),
                language_label(r),
                f"{len(r.speakers)} ({_unnamed(r)} to name)"
                if _unnamed(r)
                else str(len(r.speakers)),
                r.summary_status,
            )
        console.print(table)

    @meet_app.command("show")
    def meet_show(
        ctx: typer.Context,
        meeting: Annotated[str, typer.Argument(help="Meeting id (or a unique part of it).")],
        transcript: Annotated[bool, typer.Option(help="Show the transcript.")] = False,
    ) -> None:
        """Print a meeting's summary (or transcript)."""
        from dossier.meet.pipeline import resolve_meeting

        state = get_state(ctx)
        workspace = state.workspace()
        require_not_prohibited(workspace.config, "meetings")
        folder = resolve_meeting(workspace, meeting)
        path = folder / ("transcript.md" if transcript else "summary.md")
        text = path.read_text(encoding="utf-8")
        if state.json:
            emit(
                state,
                {
                    "meeting": folder.name,
                    "path": str(path.relative_to(workspace.root)),
                    "text": text,
                },
            )
        else:
            console.print(text, markup=False, highlight=False)

    @meet_app.command("speakers")
    def meet_speakers(
        ctx: typer.Context,
        meeting: Annotated[str, typer.Argument()],
        set_: Annotated[
            list[str] | None, typer.Option("--set", help='Confirm a name: SPEAKER_01="Alex"')
        ] = None,
    ) -> None:
        """Show speakers, or confirm who a speaker label is."""
        from dossier.meet.pipeline import confirm_speakers, load_record, resolve_meeting
        from dossier.meet.pipeline import speakers_summary as summarize_speakers

        state = get_state(ctx)
        workspace = state.workspace()
        folder = resolve_meeting(workspace, meeting)
        notes: list[str] = []
        if set_:
            require_processing(workspace.config, "speaker confirmation")
            mapping = {}
            for item in set_:
                label, sep, name = item.partition("=")
                if not sep or not name.strip():
                    raise DossierError(f"use LABEL=Name, got {item!r}")
                mapping[label.strip()] = name.strip().strip('"')
            record, notes = confirm_speakers(workspace, folder, mapping)
        else:
            record = load_record(folder)
        rows = summarize_speakers(record)
        if state.json:
            emit(state, {"meeting": record.id, "speakers": rows, "notes": notes})
            return
        for row in rows:
            console.print(f"{row['label']}: {row['display']} ({row['status']})", highlight=False)
        for note in notes:
            console.print(f"[dim]{note}[/dim]")

    @meet_app.command("review")
    def meet_review(
        ctx: typer.Context,
        meeting: Annotated[str, typer.Argument(help="Meeting id (or a unique part of it).")],
        open_page: Annotated[
            bool, typer.Option("--open", help="Open the review page in the browser.")
        ] = False,
        include_confirmed: Annotated[
            bool, typer.Option("--all", help="Also include speakers already named.")
        ] = False,
        clips: Annotated[int, typer.Option(min=1, max=6, help="Clips per speaker.")] = 3,
        recording: Annotated[
            Path | None, typer.Option(help="The recording, if it is no longer where it was.")
        ] = None,
    ) -> None:
        """Cut short clips of each unnamed speaker and check the audio, ready for a person."""
        import webbrowser

        from dossier.meet.pipeline import resolve_meeting
        from dossier.meet.review import build_review, review_dir

        state = get_state(ctx)
        workspace = state.workspace()
        require_processing(workspace.config, "speaker review")
        folder = resolve_meeting(workspace, meeting)
        with WorkspaceLock(workspace.paths.locks, "meet"):
            review = build_review(
                workspace,
                folder,
                recording=recording.resolve() if recording else None,
                include_confirmed=include_confirmed,
                clips_per_speaker=clips,
            )
        if review is None:
            if state.json:
                emit(state, {"meeting": folder.name, "speakers": [], "note": "all named"})
            else:
                console.print("Every speaker is already named (use --all to review them anyway).")
            return
        page = review_dir(folder) / "review.html"
        answers = review_dir(folder) / "answers.toml"
        if open_page:
            webbrowser.open(page.resolve().as_uri())
        if state.json:
            emit(
                state,
                {
                    **review.model_dump(),
                    "page": str(page.relative_to(workspace.root)),
                    "answers": str(answers.relative_to(workspace.root)),
                },
            )
            return
        _print_review(review)
        console.print(f"\nReview page: {page}", highlight=False)
        console.print(f"Answers file: {answers}", highlight=False)
        console.print(
            f"Next: [bold]dsr meet identify {review.meeting}[/bold] (listen and answer here), "
            f"or fill in the page or the file and run "
            f"[bold]dsr meet identify {review.meeting} --answers <file>[/bold]"
        )

    @meet_app.command("identify")
    def meet_identify(
        ctx: typer.Context,
        meeting: Annotated[str, typer.Argument(help="Meeting id (or a unique part of it).")],
        answers: Annotated[
            Path | None,
            typer.Option(help="Apply a filled-in answers file (from the review page or folder)."),
        ] = None,
        include_confirmed: Annotated[
            bool, typer.Option("--all", help="Also ask about speakers already named.")
        ] = False,
        recording: Annotated[
            Path | None, typer.Option(help="The recording, if it is no longer where it was.")
        ] = None,
    ) -> None:
        """Name the speakers: listen to their clips and answer, or apply an answers file."""
        from dossier.meet.pipeline import load_record, resolve_meeting
        from dossier.meet.review import (
            apply_answers,
            build_review,
            dump_answers,
            load_answers,
            load_review,
            review_dir,
        )

        state = get_state(ctx)
        workspace = state.workspace()
        require_processing(workspace.config, "speaker identification")
        folder = resolve_meeting(workspace, meeting)
        if answers is not None:
            with WorkspaceLock(workspace.paths.locks, "meet"):
                record, notes = apply_answers(workspace, folder, load_answers(answers))
            _print_identified(state, record, notes)
            return
        if state.json:
            raise DossierError("identify asks questions; with --json, pass --answers <file>")
        review = load_review(folder)
        record = load_record(folder)
        wanted = {
            label
            for label, info in record.speakers.items()
            if include_confirmed or info.status != "confirmed"
        }
        if not wanted:
            console.print("Every speaker is already named (use --all to go over them again).")
            return
        complete = review is not None and wanted <= {s.label for s in review.speakers}
        if complete and review is not None:
            complete = all(
                (review_dir(folder) / clip.file).is_file()
                for speaker in review.speakers
                for clip in speaker.clips
            )
        if not complete:
            console.print("Cutting clips of each speaker...")
            with WorkspaceLock(workspace.paths.locks, "meet"):
                review = build_review(
                    workspace,
                    folder,
                    recording=recording.resolve() if recording else None,
                    include_confirmed=include_confirmed,
                )
        if review is None:
            console.print("Every speaker is already named.")
            return
        speakers = [s for s in review.speakers if s.label in wanted]
        collected = _interview(review, speakers, review_dir(folder))
        (review_dir(folder) / "answers.toml").write_text(dump_answers(collected), encoding="utf-8")
        with WorkspaceLock(workspace.paths.locks, "meet"):
            record, notes = apply_answers(workspace, folder, collected)
        _print_identified(state, record, notes)

    @meet_app.command("summarize")
    def meet_summarize(ctx: typer.Context, meeting: Annotated[str, typer.Argument()]) -> None:
        """Re-run the summary (e.g. after it was degraded, or with another model)."""
        from dossier.meet.pipeline import resolve_meeting, resummarize

        state = get_state(ctx)
        workspace = state.workspace()
        require_processing(workspace.config, "meeting summary")
        record = resummarize(workspace, resolve_meeting(workspace, meeting))
        if state.json:
            emit(state, {"meeting": record.id, "summary_status": record.summary_status})
        else:
            console.print(f"{record.id}: summary {record.summary_status}")

    @meet_app.command("brief")
    def meet_brief(ctx: typer.Context, meeting: Annotated[str, typer.Argument()]) -> None:
        """Write brief.md: summary items, slides and related documents for preparation."""
        from dossier.meet.collect import build_brief
        from dossier.meet.pipeline import resolve_meeting

        state = get_state(ctx)
        workspace = state.workspace()
        require_not_prohibited(workspace.config, "brief")
        folder = resolve_meeting(workspace, meeting)
        text = build_brief(workspace, folder.name)
        if state.json:
            emit(
                state,
                {"meeting": folder.name, "path": f"meetings/{folder.name}/brief.md", "text": text},
            )
        else:
            console.print(text, markup=False, highlight=False)

    @meet_app.command("eval")
    def meet_eval(
        ctx: typer.Context,
        target: Annotated[str, typer.Argument(help="Meeting id or a transcript file.")],
        glossary: Annotated[
            Path | None, typer.Option(help="Glossary (default: workspace).")
        ] = None,
    ) -> None:
        """Score a transcript: glossary terms, known mishearings, hallucination loops."""
        from dossier.meet.eval import score
        from dossier.meet.glossary import Glossary
        from dossier.meet.pipeline import load_record, resolve_meeting

        state = get_state(ctx)
        workspace = state.maybe_workspace()
        path = Path(target)
        language: str | None = None
        if not path.is_file():
            if workspace is None:
                raise DossierError(f"{target} is not a file and there is no workspace")
            folder = resolve_meeting(workspace, target)
            path = folder / "transcript.md"
            language = load_record(folder).language
        glossary_path = glossary
        if glossary_path is None and workspace is not None and workspace.config.meet.glossary:
            glossary_path = workspace.paths.resolve(workspace.config.meet.glossary)
        result = score(
            path.read_text(encoding="utf-8"), Glossary.load(glossary_path).for_language(language)
        )
        if state.json:
            emit(state, result.as_dict())
            return
        console.print(
            f"score {result.score}  term recall {result.term_recall:.0%}  "
            f"bad forms {len(result.bad_forms)}  loop words {result.loop_words}"
        )
        if result.terms_missing:
            console.print("missing terms: " + ", ".join(result.terms_missing))

    @people_app.command("list")
    def people_list(ctx: typer.Context) -> None:
        """People with recorded consent and voiceprints."""
        from dossier.meet import people

        state = get_state(ctx)
        workspace = state.workspace()
        data = people.load(workspace.paths)
        rows = [
            {"name": p.name, "consent_at": p.consent_at, "samples": p.samples}
            for p in data.people.values()
        ]
        if state.json:
            emit(state, {"enabled": workspace.config.meet.speaker_voiceprints, "people": rows})
            return
        if not workspace.config.meet.speaker_voiceprints:
            console.print("voiceprints are off ([meet] speaker_voiceprints = false)")
        for row in rows:
            console.print(
                f"{row['name']}: consent {row['consent_at'] or 'none'}, {row['samples']} sample(s)"
            )

    @people_app.command("consent")
    def people_consent(
        ctx: typer.Context,
        name: Annotated[str, typer.Argument()],
        note: Annotated[str, typer.Option(help="How consent was given (no other personal data).")],
    ) -> None:
        """Record that a person agreed to a voiceprint being kept in this workspace."""
        from dossier.meet import people

        state = get_state(ctx)
        workspace = state.workspace()
        if not workspace.config.meet.speaker_voiceprints:
            raise DossierError("voiceprints are off; set [meet] speaker_voiceprints = true first")
        person = people.record_consent(workspace.paths, name, note)
        emit(state, {"name": person.name, "consent_at": person.consent_at})
        if not state.json:
            console.print(f"consent recorded for {person.name}")

    @people_app.command("forget")
    def people_forget(ctx: typer.Context, name: Annotated[str, typer.Argument()]) -> None:
        """Delete everything stored about a person (consent and voiceprint)."""
        from dossier.meet import people

        state = get_state(ctx)
        removed = people.forget(state.workspace().paths, name)
        emit(state, {"name": name, "removed": removed})
        if not state.json:
            console.print("removed" if removed else "not found")


def _unnamed(record: MeetingRecord) -> int:
    return sum(1 for info in record.speakers.values() if info.status != "confirmed")


def _print_review(review: Review) -> None:
    console.print(
        f"[bold]{review.title}[/bold] ({review.date}) · recording: {review.audio.grade}"
        + (f" ({'; '.join(review.audio.issues)})" if review.audio.issues else ""),
        highlight=False,
    )
    table = Table("speaker", "suggestion", "speech", "clean", "clips", "audio", "notes")
    for speaker in review.speakers:
        suggestion = (
            f"{speaker.suggestion} ({speaker.confidence:.2f})"
            if speaker.suggestion and speaker.confidence is not None
            else speaker.confirmed or ""
        )
        table.add_row(
            speaker.label,
            suggestion,
            clock(speaker.seconds),
            clock(speaker.clean_seconds),
            str(len(speaker.clips)),
            speaker.grade,
            "; ".join(speaker.issues),
        )
    console.print(table)


def _interview(review: Review, speakers: list[SpeakerReport], clips_dir: Path) -> Answers:
    """Play each speaker's clips and ask who it is and how the audio sounds."""
    from dossier import platform
    from dossier.meet.review import Answers, SpeakerAnswer, default_name

    player = platform.current().player
    can_play = player.available()
    names = list(review.names)
    console.print(
        f"[bold]{review.title}[/bold] ({review.date}): {len(speakers)} speaker(s) to name. "
        f"Recording: {review.audio.grade}"
        + (f" ({'; '.join(review.audio.issues)})" if review.audio.issues else ""),
        highlight=False,
    )
    if not can_play:
        console.print(
            f"[yellow]Cannot play audio here ({player.detail()}); open the clips in "
            f"{clips_dir}[/yellow]",
            highlight=False,
        )

    def play(speaker: SpeakerReport, index: int) -> None:
        clip = speaker.clips[index]
        console.print(
            f"  clip {index + 1}/{len(speaker.clips)} [{clock(clip.start)}] {clip.grade}"
            + (f" ({'; '.join(clip.issues)})" if clip.issues else "")
            + f"\n  “{clip.text}”",
            highlight=False,
        )
        if can_play:
            try:
                player.play(clips_dir / clip.file)
            except DossierError as exc:
                console.print(f"[yellow]  cannot play: {exc.message}[/yellow]", highlight=False)

    result: dict[str, SpeakerAnswer] = {}
    for speaker in speakers:
        console.rule(f"{speaker.label} · {clock(speaker.seconds)} of speech", align="left")
        if speaker.suggestion and speaker.confidence is not None:
            console.print(f"  suggested by voice: {speaker.suggestion} ({speaker.confidence:.2f})")
        for issue in speaker.issues:
            console.print(f"  [dim]{issue}[/dim]", highlight=False)
        index = 0
        if speaker.clips:
            play(speaker, index)
        default = default_name(speaker)
        while True:
            if names:
                console.print(
                    "  " + "  ".join(f"[bold]{n}[/bold] {name}" for n, name in enumerate(names, 1)),
                    highlight=False,
                )
            reply = typer.prompt(
                "  Who is this? (number or name"
                + (f", Enter = {default}" if default else ", Enter = don't know")
                + ", r = replay, n = next clip)",
                default="",
                show_default=False,
            ).strip()
            if reply.lower() in {"r", "n"} and speaker.clips:
                if reply.lower() == "n":
                    index = (index + 1) % len(speaker.clips)
                play(speaker, index)
                continue
            if reply.isdigit():
                if 1 <= int(reply) <= len(names):
                    name = names[int(reply) - 1]
                    break
                console.print("  [yellow]no such number[/yellow]")
                continue
            name = reply or default
            break
        quality = _ask_grade("  Audio", speaker.grade)
        result[speaker.label] = SpeakerAnswer(name=name, quality=quality)
        if name and name not in names:
            names.append(name)
    audio = _ask_grade("Overall audio of the recording", review.audio.grade)
    return Answers(meeting=review.meeting, audio=audio, speakers=result)


def _ask_grade(question: str, default: str) -> Grade:
    grades: dict[str, Grade] = {
        "good": "good",
        "fair": "fair",
        "poor": "poor",
        "g": "good",
        "f": "fair",
        "p": "poor",
    }
    while True:
        reply = str(typer.prompt(f"{question}: good / fair / poor", default=default))
        grade = grades.get(reply.strip().lower())
        if grade is not None:
            return grade
        console.print("  [yellow]answer good, fair or poor (g/f/p)[/yellow]")


def _print_identified(state: State, record: MeetingRecord, notes: list[str]) -> None:
    from dossier.meet.pipeline import speakers_summary

    if state.json:
        emit(
            state,
            {
                "meeting": record.id,
                "review_status": record.review_status,
                "audio_quality": record.audio_quality,
                "speakers": speakers_summary(record),
                "notes": notes,
            },
        )
        return
    for info in record.speakers.values():
        rating = f", audio {info.quality}" if info.quality else ""
        console.print(f"{info.label}: {info.display()} ({info.status}{rating})", highlight=False)
    for note in notes:
        console.print(f"[dim]{note}[/dim]", highlight=False)
    if record.review_status == "pending":
        console.print(f"Some speakers are still unnamed: dsr meet identify {record.id}")
