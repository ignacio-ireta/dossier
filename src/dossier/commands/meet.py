"""Meeting commands: ``dsr meet ...`` and ``dsr people ...``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from dossier.commands.common import console, emit, get_state
from dossier.core.errors import DossierError
from dossier.core.lock import WorkspaceLock
from dossier.core.policy import require_not_prohibited, require_processing
from dossier.meet.models import clock

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
                str(len(r.speakers)),
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
