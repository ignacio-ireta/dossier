"""MCP tools for documents: list template packs; render only when allowed."""

from __future__ import annotations

from typing import Any

from dossier.agent.mcp_server import EXTENSIONS, WorkspaceGetter
from dossier.core.errors import DossierError
from dossier.core.policy import require_not_prohibited


def register(server: Any, get_workspace: WorkspaceGetter) -> None:
    from mcp.types import ToolAnnotations

    from dossier.docs.packs import list_packs

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    def templates() -> dict[str, Any]:
        """Template packs and their kinds; render with template "<pack>:<kind>"."""
        workspace = get_workspace()
        return {
            "default": workspace.config.docs.template,
            "packs": [
                {
                    "name": p.name,
                    "origin": p.origin,
                    "extends": p.spec.extends,
                    "kinds": p.kind_names(),
                    "default_kind": p.spec.default_kind,
                }
                for p in list_packs(workspace)
            ],
        }

    if not get_workspace().config.agent.allow_render:
        return

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, open_world_hint=False
        )
    )
    def render_document(
        markdown_path: str, to: str = "docx", template: str | None = None
    ) -> dict[str, Any]:
        """Render a Markdown file inside the workspace to docx, pptx or pdf in out/."""
        from dossier.docs.render import render_document as render

        workspace = get_workspace()
        require_not_prohibited(workspace.config, "rendering")
        source = (workspace.root / markdown_path).resolve()
        if not source.is_relative_to(workspace.root.resolve()):
            raise DossierError("markdown_path must be inside the workspace")
        return render(workspace, source, to, template=template).as_dict()


EXTENSIONS.append(register)
