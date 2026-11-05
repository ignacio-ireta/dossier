"""Document commands: ``dsr docs render`` and ``dsr templates ...``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from dossier.commands.common import console, emit, get_state
from dossier.core.errors import EX_FAILURE, DossierError
from dossier.core.policy import require_not_prohibited

docs_app = typer.Typer(help="Render Markdown to DOCX, PPTX or PDF.", no_args_is_help=True)
templates_app = typer.Typer(help="Per-account template packs.", no_args_is_help=True)


def register(app: typer.Typer) -> None:
    app.add_typer(docs_app, name="docs")
    app.add_typer(templates_app, name="templates")

    @docs_app.command("render")
    def docs_render(
        ctx: typer.Context,
        source: Annotated[Path, typer.Argument(help="Markdown file.")],
        to: Annotated[str, typer.Option("--to", help="docx, pptx or pdf")] = "docx",
        template: Annotated[
            str | None, typer.Option(help="Template pack and kind, e.g. acme:proposal")
        ] = None,
        out: Annotated[Path | None, typer.Option(help="Output file (default: out/).")] = None,
        via_docx: Annotated[
            bool, typer.Option(help="PDF through the Word template and LibreOffice.")
        ] = False,
    ) -> None:
        """Render a Markdown file with the workspace's (or a chosen) template pack."""
        from dossier.docs.render import render_document

        state = get_state(ctx)
        workspace = state.maybe_workspace()
        if workspace is not None:
            require_not_prohibited(workspace.config, "rendering")
        result = render_document(
            workspace, source.resolve(), to, template=template, out=out, via_docx=via_docx
        )
        if state.json:
            emit(state, result.as_dict())
            return
        console.print(
            f"{result.output}  ({result.pack}:{result.kind}, {result.diagrams} diagram(s))",
            highlight=False,
        )
        for warning in result.warnings:
            console.print(f"[yellow]warning[/yellow] {warning}")

    @templates_app.command("list")
    def templates_list(ctx: typer.Context) -> None:
        """Template packs available here (workspace, user, built-in)."""
        from dossier.docs.packs import list_packs

        state = get_state(ctx)
        packs = list_packs(state.maybe_workspace())
        rows = [
            {
                "name": p.name,
                "origin": p.origin,
                "extends": p.spec.extends,
                "kinds": p.kind_names(),
                "default_kind": p.spec.default_kind,
                "path": str(p.root),
            }
            for p in packs
        ]
        if state.json:
            emit(state, rows)
            return
        table = Table("pack", "origin", "extends", "kinds", "default")
        for p in packs:
            table.add_row(
                p.name,
                p.origin,
                p.spec.extends or "",
                ", ".join(p.kind_names()),
                p.spec.default_kind,
            )
        console.print(table)

    @templates_app.command("show")
    def templates_show(
        ctx: typer.Context, pack: Annotated[str, typer.Argument(help="pack or pack:kind")]
    ) -> None:
        """What each kind of a pack resolves to, after inheritance."""
        from dossier.docs.packs import load_pack, parse_reference

        state = get_state(ctx)
        name, only = parse_reference(pack)
        loaded = load_pack(name or "neutral", state.maybe_workspace())
        data = {}
        for kind in [only] if only else loaded.kind_names():
            resolved = loaded.resolve_kind(kind)
            data[kind] = {
                "docx": str(resolved.docx) if resolved.docx else None,
                "cover": str(resolved.cover) if resolved.cover else None,
                "pptx": str(resolved.pptx) if resolved.pptx else None,
                "pdf_theme": str(resolved.pdf_theme) if resolved.pdf_theme else None,
                "pdf_vars": resolved.pdf_vars,
                "toc": resolved.toc,
                "number_sections": resolved.number_sections,
            }
        payload = {
            "pack": loaded.name,
            "chain": [p.name for p in loaded.chain()],
            "defaults": loaded.defaults(),
            "kinds": data,
        }
        if state.json:
            emit(state, payload)
            return
        chain = " -> ".join(p.name for p in loaded.chain())
        console.print(f"[bold]{loaded.name}[/bold] ({chain})")
        for kind, spec in data.items():
            console.print(f"  {kind}:", highlight=False)
            for key, value in spec.items():
                if value:
                    console.print(f"    {key}: {value}", highlight=False)

    @templates_app.command("inspect")
    def templates_inspect(ctx: typer.Context, pack: Annotated[str, typer.Argument()]) -> None:
        """Check a pack's files: styles, tokens, slide layouts, PDF theme."""
        from dossier.docs.packs import inspect_pack, load_pack

        state = get_state(ctx)
        findings = inspect_pack(load_pack(pack, state.maybe_workspace()))
        if state.json:
            emit(state, [f.__dict__ for f in findings])
        else:
            style = {"ok": "green", "warn": "yellow", "error": "red"}
            for finding in findings:
                color = style[finding.level]
                console.print(
                    f"[{color}]{finding.level:5}[/{color}] {finding.kind}: {finding.message}",
                    highlight=False,
                )
        if any(f.level == "error" for f in findings):
            raise typer.Exit(EX_FAILURE)

    @templates_app.command("new")
    def templates_new(
        ctx: typer.Context,
        name: Annotated[str, typer.Argument()],
        extends: Annotated[str, typer.Option(help="Pack to inherit from.")] = "neutral",
    ) -> None:
        """Create an empty pack in this workspace that inherits from another."""
        from dossier.docs.packs import new_pack

        state = get_state(ctx)
        root = new_pack(state.workspace(), name, extends=extends or None)
        emit(state, {"pack": name, "path": str(root)})
        if not state.json:
            console.print(f"created {root}")

    @templates_app.command("import")
    def templates_import(
        ctx: typer.Context,
        file: Annotated[Path, typer.Argument(help=".docx/.dotx, .pptx/.potx or .typ")],
        pack: Annotated[str, typer.Option(help="Pack name (created if missing).")],
        kind: Annotated[str, typer.Option(help="Document kind, e.g. report, proposal, deck.")],
        role: Annotated[
            str | None, typer.Option(help="docx, cover, pptx or pdf_theme (default: by type)")
        ] = None,
        replace: Annotated[
            list[str] | None,
            typer.Option(help='Turn text into a token: "Old title={{title}}" (repeatable).'),
        ] = None,
    ) -> None:
        """Copy an account's branded file into a workspace pack, then check it."""
        from dossier.docs.packs import import_file, inspect_pack, load_pack

        state = get_state(ctx)
        workspace = state.workspace()
        replacements: dict[str, str] = {}
        for item in replace or []:
            literal, sep, token = item.rpartition("=")
            if not sep or not literal:
                raise DossierError(f'use "literal text={{{{token}}}}", got {item!r}')
            replacements[literal] = token
        target = import_file(
            workspace, file.resolve(), pack=pack, kind=kind, role=role, replacements=replacements
        )
        findings = [f for f in inspect_pack(load_pack(pack, workspace)) if f.kind == kind]
        if state.json:
            emit(state, {"imported": str(target), "findings": [f.__dict__ for f in findings]})
            return
        console.print(f"imported {target}")
        for finding in findings:
            console.print(f"  {finding.level}: {finding.message}", highlight=False)
