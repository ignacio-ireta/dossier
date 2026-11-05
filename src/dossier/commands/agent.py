"""Agent commands: ``dsr mcp serve|install``, ``dsr skill path|install``."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from importlib import resources
from pathlib import Path
from typing import Annotated

import typer

from dossier.commands.common import console, emit, get_state
from dossier.core.errors import ExternalToolError
from dossier.ops.schedule import dsr_command

mcp_app = typer.Typer(help="MCP server for agents (read-only by default).", no_args_is_help=True)
skill_app = typer.Typer(help="The dossier skill for Claude Code.", no_args_is_help=True)


def skill_source() -> Path:
    return Path(str(resources.files("dossier.agent") / "skill"))


def server_name(workspace_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", workspace_name.lower()).strip("-")
    return f"dossier-{slug or 'workspace'}"


def register(app: typer.Typer) -> None:
    app.add_typer(mcp_app, name="mcp")
    app.add_typer(skill_app, name="skill")

    @mcp_app.command("serve")
    def mcp_serve(ctx: typer.Context) -> None:
        """Run the MCP server on stdio for this workspace."""
        from dossier.agent.mcp_server import serve

        serve(get_state(ctx).workspace_path)

    @mcp_app.command("install")
    def mcp_install(
        ctx: typer.Context,
        client: Annotated[str, typer.Option(help="claude-code, or json for other clients")] = (
            "claude-code"
        ),
        scope: Annotated[str, typer.Option(help="claude-code scope: local, project, user")] = (
            "local"
        ),
        yes: Annotated[bool, typer.Option("--yes", "-y", help="Run the command.")] = False,
    ) -> None:
        """Print (or with --yes, run) the command that registers this workspace's server."""
        state = get_state(ctx)
        workspace = state.workspace()
        name = server_name(workspace.config.workspace.name)
        command = [*dsr_command(), "--workspace", str(workspace.root), "mcp", "serve"]
        if client == "json":
            snippet = {"mcpServers": {name: {"command": command[0], "args": command[1:]}}}
            if state.json:
                emit(state, snippet)
            else:
                console.print(json.dumps(snippet, indent=2), markup=False)
            return
        if client != "claude-code":
            raise typer.BadParameter("client must be claude-code or json")
        register_cmd = ["claude", "mcp", "add", "--scope", scope, name, "--", *command]
        if state.json and not yes:
            emit(state, {"command": register_cmd})
            return
        console.print(" ".join(register_cmd), markup=False)
        if not yes:
            console.print("Re-run with --yes to run it.")
            return
        if not shutil.which("claude"):
            raise ExternalToolError("the claude command is not on PATH")
        completed = subprocess.run(register_cmd, check=False)
        raise typer.Exit(completed.returncode)

    @skill_app.command("path")
    def skill_path(ctx: typer.Context) -> None:
        """Where the bundled skill lives."""
        state = get_state(ctx)
        path = skill_source()
        if state.json:
            emit(state, {"path": str(path)})
        else:
            console.print(str(path))

    @skill_app.command("install")
    def skill_install(
        ctx: typer.Context,
        dest: Annotated[Path, typer.Option(help="Skills folder.")] = Path("~/.claude/skills"),
        yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
    ) -> None:
        """Copy the skill into a Claude Code skills folder."""
        state = get_state(ctx)
        target = dest.expanduser() / "dossier"
        if not yes and not typer.confirm(f"Copy the dossier skill to {target}?", default=True):
            raise typer.Exit(0)
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_source() / "SKILL.md", target / "SKILL.md")
        if state.json:
            emit(state, {"installed": str(target)})
        else:
            console.print(f"installed {target}")
