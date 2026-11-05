"""The ``dsr`` command line. Every command supports ``--json`` for agents."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from dossier import __version__
from dossier.commands import corpus as corpus_commands
from dossier.commands import knowledge as knowledge_commands
from dossier.commands.common import State, console, emit, err_console, get_state
from dossier.core import logging as dlogging
from dossier.core.config import ProcessingStatus
from dossier.core.errors import EX_FAILURE, DossierError
from dossier.core.policy import set_processing
from dossier.core.secrets import delete_secret, set_secret
from dossier.llm.router import ModelRouter
from dossier.ops import doctor as doctor_ops
from dossier.ops import models as model_ops
from dossier.ops import setup as setup_ops
from dossier.ops.init import DEFAULT_CHAT_MODEL, DEFAULT_EMBED_MODEL, init_workspace
from dossier.ops.tools import FEATURES

app = typer.Typer(
    name="dsr",
    help="dossier: keep a project's context converted, summarized and searchable for agents.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
policy_app = typer.Typer(
    help="Show or change what this workspace may process.", no_args_is_help=True
)
models_app = typer.Typer(help="Inspect and test the model roles.", no_args_is_help=True)
secrets_app = typer.Typer(help="Store API keys and tokens in the OS keyring.", no_args_is_help=True)
app.add_typer(policy_app, name="policy")
app.add_typer(models_app, name="models")
app.add_typer(secrets_app, name="secrets")


@app.callback()
def _root(
    ctx: typer.Context,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace folder (default: search upward)."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    log_level: Annotated[str, typer.Option("--log-level", help="Console log level.")] = "WARNING",
) -> None:
    ctx.obj = State(workspace_path=workspace, json=json_output)
    dlogging.configure(log_level)


@app.command()
def version(ctx: typer.Context) -> None:
    """Print the dossier version."""
    state = get_state(ctx)
    if state.json:
        emit(state, {"version": __version__})
    else:
        console.print(__version__)


@app.command()
def init(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="Folder for the new workspace.")] = Path(),
    name: Annotated[str | None, typer.Option(help="Workspace name (default: folder name).")] = None,
    chat_model: Annotated[str, typer.Option(help="Model for summaries.")] = DEFAULT_CHAT_MODEL,
    embed_model: Annotated[str, typer.Option(help="Model for embeddings.")] = DEFAULT_EMBED_MODEL,
    force: Annotated[bool, typer.Option(help="Overwrite an existing dossier.toml.")] = False,
) -> None:
    """Create a workspace: folders plus a commented dossier.toml (policy: pending)."""
    state = get_state(ctx)
    root = path.resolve()
    paths = init_workspace(
        root,
        name=name or root.name,
        chat_model=chat_model,
        embed_model=embed_model,
        force=force,
    )
    if state.json:
        emit(state, {"workspace": str(paths.root), "config": str(paths.config)})
        return
    console.print(f"Created workspace at [bold]{paths.root}[/bold]")
    console.print("Next steps:")
    console.print(f"  1. Edit {paths.config.name}: write a short brief, check \\[\\[sources]].")
    console.print("  2. dsr doctor        (what is installed, which models are reachable)")
    console.print("  3. dsr policy approve (once processing this material here is permitted)")


_LEVEL_STYLE = {"ok": "green", "info": "cyan", "warn": "yellow", "fail": "red"}


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check runtime, platform backends, external tools, extras, and the workspace."""
    state = get_state(ctx)
    checks = doctor_ops.run_doctor(state.maybe_workspace())
    if state.json:
        emit(state, [check.as_dict() for check in checks])
    else:
        table = Table(show_header=True, header_style="bold")
        for column in ("group", "check", "status", "detail", "fix"):
            table.add_column(column, overflow="fold")
        for check in checks:
            style = _LEVEL_STYLE[check.level.value]
            table.add_row(
                check.group,
                check.name,
                f"[{style}]{check.level.value}[/{style}]",
                check.detail,
                check.fix,
            )
        console.print(table)
    if any(check.level is doctor_ops.Level.FAIL for check in checks):
        raise typer.Exit(EX_FAILURE)


@app.command()
def setup(
    ctx: typer.Context,
    with_: Annotated[
        str,
        typer.Option("--with", help=f"Comma-separated features: {', '.join(FEATURES)}."),
    ] = "llm",
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Run without asking.")] = False,
) -> None:
    """Install the system tools and models that selected features need."""
    state = get_state(ctx)
    features = {item.strip() for item in with_.split(",") if item.strip()}
    workspace = state.maybe_workspace()
    plan = setup_ops.plan_setup(features, workspace.config if workspace else None)
    if state.json and not yes:
        emit(state, {"commands": plan.commands, "manual": plan.manual, "ok": plan.satisfied})
        return
    for line in plan.satisfied:
        console.print(f"[green]ok[/green]   {line}")
    for line in plan.manual:
        console.print(f"[yellow]todo[/yellow] {line}")
    if not plan.commands:
        console.print("Nothing to run.")
        return
    console.print("Will run:")
    for command in plan.commands:
        console.print(f"  {' '.join(command)}")
    if not yes and not typer.confirm("Run these commands?", default=False):
        raise typer.Exit(0)
    results = setup_ops.run_commands(plan.commands)
    failed = [command for command, code in results if code != 0]
    if failed:
        err_console.print(f"failed: {' '.join(failed[0])}")
        raise typer.Exit(EX_FAILURE)


@app.command()
def status(ctx: typer.Context) -> None:
    """Show the workspace, its policy, and the latest run."""
    state = get_state(ctx)
    workspace = state.workspace()
    config = workspace.config
    runs = sorted(workspace.paths.runs.glob("*/report.json"))
    latest: dict[str, Any] | None = None
    if runs:
        latest = json.loads(runs[-1].read_text(encoding="utf-8"))
    data = {
        "workspace": config.workspace.name,
        "root": str(workspace.root),
        "policy": config.policy.processing.value,
        "external_models": config.policy.allow_external,
        "sources": [source.name for source in config.sources],
        "latest_run": latest,
    }
    if state.json:
        emit(state, data)
        return
    console.print(f"[bold]{config.workspace.name}[/bold]  {workspace.root}")
    console.print(
        f"policy: processing={config.policy.processing.value}, "
        f"external models={'allowed' if config.policy.allow_external else 'off'}"
    )
    console.print(f"sources: {', '.join(data['sources']) or 'none'}")  # type: ignore[arg-type]
    if latest:
        console.print(
            f"latest run: {latest['command']} {latest['run_id']} exit={latest['exit_status']}"
        )
    else:
        console.print("latest run: none yet")


@policy_app.command("show")
def policy_show(ctx: typer.Context) -> None:
    """Print the policy block."""
    state = get_state(ctx)
    policy = state.workspace().config.policy
    data = policy.model_dump(mode="json")
    if state.json:
        emit(state, data)
    else:
        for key, value in data.items():
            console.print(f"{key} = {value}")


@policy_app.command("approve")
def policy_approve(
    ctx: typer.Context,
    note: Annotated[str, typer.Option(help="Why processing is permitted (no personal data).")] = "",
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    """Permit dossier to read and process this workspace's sources on this machine."""
    state = get_state(ctx)
    workspace = state.workspace()
    if not yes and not state.json:
        console.print(
            "Approving lets dossier read, convert, index and transcribe the sources of "
            f"[bold]{workspace.config.workspace.name}[/bold] on this machine. Content goes "
            "only to the model providers the policy allows."
        )
        if not typer.confirm("Approve processing?", default=False):
            raise typer.Exit(0)
    set_processing(workspace.paths.config, ProcessingStatus.APPROVED, note=note)
    emit(state, {"processing": "approved"})
    if not state.json:
        console.print("processing = approved")


@policy_app.command("prohibit")
def policy_prohibit(
    ctx: typer.Context,
    note: Annotated[str, typer.Option(help="Reason (no personal data).")] = "",
) -> None:
    """Stop all processing of this workspace's sources."""
    state = get_state(ctx)
    workspace = state.workspace()
    set_processing(workspace.paths.config, ProcessingStatus.PROHIBITED, note=note)
    emit(state, {"processing": "prohibited"})
    if not state.json:
        console.print("processing = prohibited")


@models_app.command("list")
def models_list(ctx: typer.Context) -> None:
    """Show which provider and model each role uses."""
    state = get_state(ctx)
    rows = model_ops.describe_roles(state.workspace().config)
    if state.json:
        emit(state, rows)
        return
    table = Table("role", "provider", "model", "context", "output", "think")
    for row in rows:
        if not row["configured"]:
            table.add_row(str(row["role"]), "-", "[dim]not configured[/dim]", "", "", "")
        elif not row["enabled"]:
            table.add_row(
                str(row["role"]),
                str(row["provider"]),
                f"[dim]{row['model']} (off)[/dim]",
                "",
                "",
                "",
            )
        else:
            table.add_row(
                str(row["role"]),
                str(row["provider"]),
                str(row["model"]),
                str(row["context_tokens"]),
                str(row["max_output_tokens"]),
                str(row["think"]),
            )
    console.print(table)


@models_app.command("test")
def models_test(
    ctx: typer.Context,
    role: Annotated[list[str] | None, typer.Option(help="Roles to test (default: all).")] = None,
) -> None:
    """Send a tiny probe (no workspace content) to each role and report the result."""
    state = get_state(ctx)
    config = state.workspace().config
    router = ModelRouter(config)
    roles = role or [name for name in config.models]
    results = [model_ops.probe_role(router, name) for name in roles]
    if state.json:
        emit(state, [result.__dict__ for result in results])
    else:
        table = Table("role", "model", "status", "seconds", "detail")
        for result in results:
            style = {"ok": "green", "repaired": "yellow", "failed": "red"}.get(
                result.status, "cyan"
            )
            table.add_row(
                result.role,
                result.model,
                f"[{style}]{result.status}[/{style}]",
                f"{result.seconds:.2f}",
                result.detail,
            )
        console.print(table)
    if any(result.status == "failed" for result in results):
        raise typer.Exit(EX_FAILURE)


@secrets_app.command("set")
def secrets_set(
    name: Annotated[str, typer.Argument(help="Secret name, e.g. hf-token or anthropic.")],
) -> None:
    """Store a secret in the OS keyring (prompted, never echoed)."""
    value = typer.prompt(f"Value for {name}", hide_input=True)
    set_secret(name, value)
    console.print(f"stored {name} in the OS keyring")


@secrets_app.command("delete")
def secrets_delete(name: Annotated[str, typer.Argument()]) -> None:
    """Remove a secret from the OS keyring."""
    console.print("deleted" if delete_secret(name) else "not found")


corpus_commands.register(app)
knowledge_commands.register(app, models_app)


def main() -> None:
    """Entry point. Click handles usage errors and exits; dossier errors map to exit codes."""
    try:
        app()
    except DossierError as exc:
        if "--json" in sys.argv:
            sys.stdout.write(json.dumps({"error": exc.as_dict()}, indent=2) + "\n")
        else:
            err_console.print(f"[red]error[/red] [{exc.code}] {exc.message}")
        raise SystemExit(exc.exit_status) from None
