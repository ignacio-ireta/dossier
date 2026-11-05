"""MCP server for one workspace (``dsr mcp serve``, stdio).

Read-only by default: every tool reads the converted corpus, the index, and
(once present) meetings. Results carry provenance (document key, line range,
paths) so an agent can cite and re-open what it used. The workspace policy is
re-read on every call: if processing is set to ``prohibited``, tools refuse.
Feature modules add their tools through :data:`EXTENSIONS`.
"""

from __future__ import annotations

import functools
import importlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dossier import __version__
from dossier.core.errors import DossierError
from dossier.core.policy import require_not_prohibited
from dossier.core.workspace import Workspace, open_workspace

WorkspaceGetter = Callable[[], Workspace]
Extension = Callable[[Any, WorkspaceGetter], None]

# Feature modules (meetings, documents) append a function that adds their tools.
EXTENSIONS: list[Extension] = []

INSTRUCTIONS = """\
This server gives read access to one dossier workspace: a project's documents
converted to Markdown, their summaries, and search.

Work like a colleague new to the project:
1. `overview` for the workspace map, then `tree` (or `tree` with a folder) to
   see what exists and where.
2. `search` for specific questions; every hit names the document and lines.
3. `card` for a document's summary and outline (with line numbers), then `read`
   with a line range for the exact text before relying on it.
Cite documents by their key and line range. Do not present summaries as quotes.
"""


def _load(get_workspace: WorkspaceGetter, action: str) -> Workspace:
    workspace = get_workspace()
    require_not_prohibited(workspace.config, action)
    return workspace


def _text(path: Path, root: Path) -> dict[str, Any]:
    if not path.is_file():
        raise DossierError(f"{path.relative_to(root)} not found; run `dsr sync` first")
    return {"path": str(path.relative_to(root)), "text": path.read_text(encoding="utf-8")}


def guard(function: Callable[..., Any]) -> Callable[..., Any]:
    """Report dossier errors to the client as tool errors with their message."""
    from mcp.server.mcpserver.exceptions import ToolError

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except DossierError as exc:
            raise ToolError(f"[{exc.code}] {exc.message}") from exc

    return wrapper


def _guarded_server_class() -> Any:
    from mcp.server.mcpserver import MCPServer

    class GuardedServer(MCPServer):
        def tool(self, *args: Any, **kwargs: Any) -> Callable[[Any], Any]:
            register = super().tool(*args, **kwargs)
            return lambda function: register(guard(function))

    return GuardedServer


def build_server(workspace_path: Path | None = None) -> Any:
    from mcp.types import ToolAnnotations

    GuardedServer = _guarded_server_class()

    def get_workspace() -> Workspace:
        return open_workspace(workspace_path)

    first = get_workspace()
    server = GuardedServer(
        name=f"dossier: {first.config.workspace.name}",
        instructions=INSTRUCTIONS,
        version=__version__,
    )
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @server.tool(annotations=read_only)
    def status() -> dict[str, Any]:
        """Workspace name, policy, freshness of the index, and the latest run."""
        workspace = _load(get_workspace, "status")
        config = workspace.config
        runs = sorted(workspace.paths.runs.glob("*/report.json"))
        latest = json.loads(runs[-1].read_text(encoding="utf-8")) if runs else None
        index_json = workspace.paths.index / "index.json"
        documents = 0
        updated = None
        if index_json.is_file():
            data = json.loads(index_json.read_text(encoding="utf-8"))
            documents, updated = len(data.get("documents", [])), data.get("updated_at")
        return {
            "workspace": config.workspace.name,
            "brief": config.workspace.brief,
            "policy": config.policy.processing.value,
            "documents_indexed": documents,
            "index_updated_at": updated,
            "latest_run": {
                "run_id": latest["run_id"],
                "finished_at": latest["finished_at"],
                "exit_status": latest["exit_status"],
            }
            if latest
            else None,
        }

    @server.tool(annotations=read_only)
    def overview() -> dict[str, Any]:
        """INDEX.md: how to navigate, sources, documents by type and topic, recent changes."""
        workspace = _load(get_workspace, "overview")
        return _text(workspace.paths.index / "INDEX.md", workspace.root)

    @server.tool(annotations=read_only)
    def tree(folder: str = "") -> dict[str, Any]:
        """TREE.md (every folder and document, one line each), or one folder's page.

        folder: e.g. "main/plans"; empty for the whole tree.
        """
        workspace = _load(get_workspace, "tree")
        if folder.strip("/"):
            path = workspace.paths.index / "folders" / folder.strip("/") / "_folder.md"
            if not path.resolve().is_relative_to(workspace.paths.index.resolve()):
                raise DossierError("folder must be inside the index")
        else:
            path = workspace.paths.index / "TREE.md"
        return _text(path, workspace.root)

    @server.tool(annotations=read_only)
    def search(
        query: str,
        limit: int = 8,
        source: str | None = None,
        path_prefix: str | None = None,
        doc_type: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """Hybrid keyword + semantic search. Each result: doc, title, heading, lines, snippet.

        doc_type: e.g. pdf, docx, xlsx, pptx, eml. kind: document or meeting.
        """
        from dossier.index.search import SearchFilters
        from dossier.index.search import search as run_search

        workspace = _load(get_workspace, "search")
        hits, notes = run_search(
            workspace,
            query,
            limit=max(1, min(limit, 50)),
            filters=SearchFilters(source, path_prefix, doc_type, kind),
        )
        return {"query": query, "notes": notes, "results": [hit.as_dict() for hit in hits]}

    @server.tool(annotations=read_only)
    def card(doc: str) -> dict[str, Any]:
        """A document's card: summary, key points, topics, outline with line numbers."""
        from dossier.index.read import read_document

        workspace = _load(get_workspace, "card")
        return read_document(workspace, doc, card=True)

    @server.tool(annotations=read_only)
    def read(doc: str, lines: str | None = None, max_chars: int = 20000) -> dict[str, Any]:
        """The converted text of a document, optionally a line range such as "40-90"."""
        from dossier.index.read import read_document

        workspace = _load(get_workspace, "read")
        return read_document(
            workspace, doc, lines=lines, max_chars=max(1000, min(max_chars, 200_000))
        )

    importlib.import_module("dossier.meet.mcp_tools")  # registers the meeting tools
    for extension in EXTENSIONS:
        extension(server, get_workspace)
    return server


def serve(workspace_path: Path | None = None) -> None:
    build_server(workspace_path).run("stdio")
