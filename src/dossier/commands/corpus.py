"""Corpus commands: ``dsr convert``, ``dsr sync``, ``dsr schedule``."""

from __future__ import annotations

from typing import Annotated, Any

import typer
from rich.table import Table

from dossier.commands.common import console, emit, get_state
from dossier.ops import schedule as schedule_ops
from dossier.ops.sync import SyncOptions, SyncOutcome, run_sync

schedule_app = typer.Typer(help="Run sync automatically (daily, on change).", no_args_is_help=True)


def _print_outcome(outcome: SyncOutcome, dry_run: bool) -> None:
    verb = "would" if dry_run else "did"
    for stage, data in outcome.stages.items():
        if "error" in data:
            console.print(f"[red]{stage} failed[/red]: {data['error']['message']}")
            continue
        if stage == "convert":
            table = Table(title=f"convert ({verb})", show_header=True, header_style="bold")
            columns = [
                "source",
                "added",
                "updated",
                "moved",
                "deleted",
                "unchanged",
                "failed",
                "media",
                "skipped",
            ]
            for column in columns:
                table.add_column(column, justify="right" if column != "source" else "left")
            for source, result in data.items():
                counts: dict[str, int] = result["counts"]
                skipped = sum(v for k, v in counts.items() if k.startswith("skipped_"))
                table.add_row(
                    source,
                    *(str(counts.get(key, 0)) for key in columns[1:-1]),
                    str(skipped),
                )
            console.print(table)
            for source, result in data.items():
                for error in result.get("errors", [])[:20]:
                    console.print(f"  [red]failed[/red] {source}/{error['path']}: {error['code']}")
        else:
            summary = ", ".join(f"{k}={v}" for k, v in _flat_counts(data).items())
            console.print(f"{stage}: {summary or 'done'}")
    for item in outcome.attention:
        console.print(f"[yellow]needs attention[/yellow] {item}")
    if not dry_run:
        console.print(f"run {outcome.report.run_id}: exit {outcome.exit_status}")


def _flat_counts(data: dict[str, Any]) -> dict[str, Any]:
    counts = data.get("counts")
    return counts if isinstance(counts, dict) else {}


def _run(ctx: typer.Context, options: SyncOptions) -> None:
    state = get_state(ctx)
    workspace = state.workspace()
    outcome = run_sync(workspace, options)
    if state.json:
        emit(
            state,
            {
                "run_id": None if options.dry_run else outcome.report.run_id,
                "dry_run": options.dry_run,
                "exit_status": outcome.exit_status,
                "attention": outcome.attention,
                "stages": outcome.stages,
            },
        )
    else:
        _print_outcome(outcome, options.dry_run)
    if outcome.exit_status:
        raise typer.Exit(outcome.exit_status)


def register(app: typer.Typer) -> None:
    app.add_typer(schedule_app, name="schedule")

    @app.command()
    def convert(
        ctx: typer.Context,
        source: Annotated[
            list[str] | None, typer.Option("--source", "-s", help="Only these sources.")
        ] = None,
        dry_run: Annotated[bool, typer.Option(help="Plan only; write nothing.")] = False,
        confirm_deletes: Annotated[
            bool, typer.Option(help="Allow a run that deletes many converted files.")
        ] = False,
        rehash: Annotated[bool, typer.Option(help="Hash every file, ignoring mtimes.")] = False,
        retry_failed: Annotated[bool, typer.Option(help="Retry files that failed.")] = False,
    ) -> None:
        """Convert new and changed source files into corpus/ (Markdown + CSV)."""
        _run(
            ctx,
            SyncOptions(
                dry_run=dry_run,
                confirm_deletes=confirm_deletes,
                rehash=rehash,
                retry_failed=retry_failed,
                sources=source,
                stages=["convert"],
            ),
        )

    @app.command()
    def sync(
        ctx: typer.Context,
        dry_run: Annotated[bool, typer.Option(help="Plan only; write nothing.")] = False,
        scheduled: Annotated[
            bool, typer.Option(help="Unattended run: local models only, notify on problems.")
        ] = False,
        confirm_deletes: Annotated[bool, typer.Option()] = False,
        stage: Annotated[
            list[str] | None, typer.Option("--stage", help="Only these stages.")
        ] = None,
    ) -> None:
        """Run every stage: convert, then index, then meetings."""
        _run(
            ctx,
            SyncOptions(
                dry_run=dry_run,
                scheduled=scheduled,
                confirm_deletes=confirm_deletes,
                stages=stage,
            ),
        )

    @schedule_app.command("install")
    def schedule_install(
        ctx: typer.Context,
        at: Annotated[str | None, typer.Option(help="Daily time, HH:MM (24-hour).")] = "07:30",
        watch: Annotated[bool, typer.Option(help="Also run when a source folder changes.")] = True,
    ) -> None:
        """Install the scheduled sync for this workspace (replaces an existing one)."""
        state = get_state(ctx)
        job, path = schedule_ops.install(state.workspace(), at=at, watch=watch)
        data = {
            "label": job.label,
            "definition": str(path),
            "daily_at": job.daily_at,
            "watch_paths": [str(p) for p in job.watch_paths],
        }
        if state.json:
            emit(state, data)
        else:
            console.print(f"installed {job.label} ({path})")

    @schedule_app.command("remove")
    def schedule_remove(ctx: typer.Context) -> None:
        """Remove this workspace's scheduled sync."""
        state = get_state(ctx)
        removed = schedule_ops.remove(state.workspace())
        if state.json:
            emit(state, {"removed": removed})
        else:
            console.print("removed" if removed else "no schedule installed")

    @schedule_app.command("status")
    def schedule_status(ctx: typer.Context) -> None:
        """Show whether the scheduled sync is installed."""
        state = get_state(ctx)
        result = schedule_ops.status(state.workspace())
        if state.json:
            emit(
                state,
                {"label": result.label, "installed": result.installed, "detail": result.detail},
            )
        else:
            console.print(
                f"{result.label}: {'installed' if result.installed else 'not installed'}"
                + (f" ({result.detail})" if result.detail else "")
            )
