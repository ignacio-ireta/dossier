"""Index and retrieval commands: index, search, tree, read, taxonomy, qmd, models bench."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated

import typer
from rich.markdown import Markdown
from rich.table import Table

from dossier.commands.common import console, emit, get_state
from dossier.commands.corpus import _run
from dossier.core.config import SearchBackend
from dossier.core.errors import DossierError
from dossier.core.policy import require_not_prohibited
from dossier.ops.sync import SyncOptions

taxonomy_app = typer.Typer(help="Topic vocabulary for document cards.", no_args_is_help=True)
qmd_app = typer.Typer(help="Optional qmd search backend.", no_args_is_help=True)


def register(app: typer.Typer, models_app: typer.Typer) -> None:
    app.add_typer(taxonomy_app, name="taxonomy")
    app.add_typer(qmd_app, name="qmd")

    @app.command()
    def index(
        ctx: typer.Context,
        force: Annotated[bool, typer.Option(help="Rebuild every card and chunk.")] = False,
        dry_run: Annotated[bool, typer.Option(help="Show what would be indexed.")] = False,
    ) -> None:
        """Refresh cards, folder summaries, TREE.md/INDEX.md and the search index."""
        _run(ctx, SyncOptions(stages=["index"], force_index=force, dry_run=dry_run))

    @app.command("search")
    def search_command(
        ctx: typer.Context,
        query: Annotated[str, typer.Argument(help="Question or keywords.")],
        limit: Annotated[int, typer.Option("--limit", "-k", min=1, max=100)] = 10,
        source: Annotated[str | None, typer.Option(help="Only this source.")] = None,
        path: Annotated[str | None, typer.Option(help="Only documents under this prefix.")] = None,
        doc_type: Annotated[str | None, typer.Option("--type", help="e.g. pdf, docx")] = None,
        kind: Annotated[str | None, typer.Option(help="document or meeting")] = None,
        backend: Annotated[SearchBackend | None, typer.Option(help="native or qmd")] = None,
    ) -> None:
        """Search the workspace (keyword + semantic). Results cite document and lines."""
        from dossier.index.search import SearchFilters, search

        state = get_state(ctx)
        workspace = state.workspace()
        require_not_prohibited(workspace.config, "search")
        chosen = backend or workspace.config.index.search_backend
        notes: list[str] = []
        if chosen is SearchBackend.QMD:
            from dossier.index.qmd import Qmd

            hits = Qmd(workspace).search(query, limit=limit)
        else:
            hits, notes = search(
                workspace,
                query,
                limit=limit,
                filters=SearchFilters(
                    source=source, path_prefix=path, doc_type=doc_type, kind=kind
                ),
            )
        if state.json:
            emit(
                state,
                {
                    "query": query,
                    "backend": chosen.value,
                    "notes": notes,
                    "results": [hit.as_dict() for hit in hits],
                },
            )
            return
        for note in notes:
            console.print(f"[dim]{note}[/dim]")
        if not hits:
            console.print("no results")
        for hit in hits:
            where = f"{hit.doc}" + (f"  lines {hit.lines}" if hit.lines else "")
            console.print(f"[bold]{hit.rank}. {hit.title}[/bold]  [cyan]{where}[/cyan]")
            if hit.heading:
                console.print(f"   [dim]{hit.heading}[/dim]")
            console.print(f"   {hit.snippet}")

    @app.command()
    def tree(
        ctx: typer.Context,
        folder: Annotated[str | None, typer.Argument(help="A folder, e.g. main/plans")] = None,
    ) -> None:
        """Show TREE.md, or one folder's summary page."""
        state = get_state(ctx)
        workspace = state.workspace()
        require_not_prohibited(workspace.config, "tree")
        if folder:
            path = workspace.paths.index / "folders" / folder.strip("/") / "_folder.md"
        else:
            path = workspace.paths.index / "TREE.md"
        if not path.is_file():
            raise DossierError(f"{path.relative_to(workspace.root)} not found; run `dsr sync`")
        text = path.read_text(encoding="utf-8")
        if state.json:
            emit(state, {"path": str(path.relative_to(workspace.root)), "text": text})
        else:
            console.print(Markdown(text))

    @app.command()
    def read(
        ctx: typer.Context,
        doc: Annotated[str, typer.Argument(help="Document key, corpus path or card path.")],
        lines: Annotated[str | None, typer.Option(help="Line range, e.g. 40-90.")] = None,
        card: Annotated[bool, typer.Option(help="Show the document card instead.")] = False,
    ) -> None:
        """Print a converted document (or part of it, or its card)."""
        from dossier.index.read import read_document

        state = get_state(ctx)
        workspace = state.workspace()
        require_not_prohibited(workspace.config, "read")
        data = read_document(workspace, doc, lines=lines, card=card)
        if state.json:
            emit(state, data)
        else:
            console.print(str(data["text"]), markup=False, highlight=False)

    @taxonomy_app.command("suggest")
    def taxonomy_suggest(ctx: typer.Context) -> None:
        """Propose topics from the cards' free tags (written to index/, not applied)."""
        from dossier.index.taxonomy import suggest

        state = get_state(ctx)
        workspace = state.workspace()
        require_not_prohibited(workspace.config, "taxonomy")
        topics, snippet = suggest(workspace)
        if state.json:
            emit(
                state,
                {
                    "topics": [{"key": k, "label": label} for k, label in topics],
                    "written": "index/taxonomy-suggestion.toml",
                },
            )
        else:
            console.print(snippet, markup=False)

    @qmd_app.command("sync")
    def qmd_sync(ctx: typer.Context) -> None:
        """Register the corpus and folder summaries with qmd, then let qmd index it."""
        from dossier.index.qmd import Qmd

        state = get_state(ctx)
        workspace = state.workspace()
        require_not_prohibited(workspace.config, "qmd sync")
        counts = Qmd(workspace).sync()
        if state.json:
            emit(state, counts)
        else:
            console.print(", ".join(f"{k}={v}" for k, v in counts.items()))

    @models_app.command("bench")
    def models_bench(
        ctx: typer.Context,
        keep: Annotated[
            Path | None, typer.Option(help="Keep the benchmark workspace in this folder.")
        ] = None,
    ) -> None:
        """Benchmark this workspace's models on a synthetic corpus (no real content)."""
        from dossier.eval.bench import run_bench

        state = get_state(ctx)
        source = state.maybe_workspace()
        if keep is not None:
            result = run_bench(source, keep.resolve())
        else:
            with tempfile.TemporaryDirectory(prefix="dossier-bench-") as tmp:
                result = run_bench(source, Path(tmp) / "bench")
        if state.json:
            emit(state, result.as_dict())
            return
        table = Table("metric", "value")
        table.add_row("index model", result.index_model)
        table.add_row("embedding model", result.embed_model)
        table.add_row("cards by model / fallback", f"{result.cards_ok} / {result.cards_degraded}")
        table.add_row("fact recall in cards", f"{result.fact_recall:.0%}")
        table.add_row("retrieval hit@5", f"{result.hit_at_5:.0%}")
        table.add_row("convert + index time", f"{result.index_seconds}s")
        table.add_row("model calls", str(result.model_calls))
        console.print(table)
        for note in result.notes:
            console.print(f"[dim]{note}[/dim]")
