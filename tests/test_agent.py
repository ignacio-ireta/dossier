from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest
from typer.testing import CliRunner

from dossier.agent.mcp_server import build_server
from dossier.agent.navigation import MARKER, write_navigation
from dossier.cli import app
from dossier.commands.agent import skill_source
from dossier.core.config import ProcessingStatus
from dossier.core.policy import set_processing
from dossier.core.workspace import Workspace, open_workspace

pytest.importorskip("mcp")
from mcp import Client, StdioServerParameters

runner = CliRunner()


def _structured(result: Any) -> dict[str, Any]:
    if getattr(result, "structured_content", None):
        data: dict[str, Any] = result.structured_content
        return data.get("result", data) if set(data) == {"result"} else data
    return json.loads(result.content[0].text)  # type: ignore[no-any-return]


async def _session(workspace: Workspace) -> dict[str, Any]:
    server = build_server(workspace.root)
    out: dict[str, Any] = {}
    async with Client(server) as client:
        tools = await client.list_tools()
        out["tools"] = {tool.name: tool for tool in tools.tools}
        out["search"] = _structured(
            await client.call_tool("search", {"query": "hosting option steering committee"})
        )
        out["tree"] = _structured(await client.call_tool("tree", {}))
        out["folder"] = _structured(await client.call_tool("tree", {"folder": "main/plans"}))
        out["card"] = _structured(
            await client.call_tool("card", {"doc": "main/plans/roadmap.pptx"})
        )
        out["read"] = _structured(
            await client.call_tool("read", {"doc": "main/plans/roadmap.pptx", "lines": "1-4"})
        )
        out["status"] = _structured(await client.call_tool("status", {}))
        out["escape"] = await client.call_tool("tree", {"folder": "../../.."})
    return out


def test_mcp_tools_in_process(indexed: Workspace) -> None:
    out = asyncio.run(_session(indexed))
    assert {"status", "overview", "tree", "search", "card", "read"} <= set(out["tools"])
    assert out["tools"]["search"].annotations.read_only_hint is True
    assert out["search"]["results"][0]["doc"] == "main/mail/steering-decision.eml"
    assert "TREE" not in out["tree"]["path"] or out["tree"]["path"] == "index/TREE.md"
    assert out["folder"]["path"] == "index/folders/main/plans/_folder.md"
    assert out["card"]["path"].startswith("index/cards/")
    assert out["read"]["lines"] == "1-4"
    assert out["status"]["documents_indexed"] == 9
    assert out["escape"].is_error


def test_mcp_refuses_when_prohibited(indexed: Workspace) -> None:
    async def call() -> Any:
        server = build_server(indexed.root)
        set_processing(indexed.paths.config, ProcessingStatus.PROHIBITED)
        async with Client(server) as client:
            return await client.call_tool("search", {"query": "budget"})

    result = asyncio.run(call())
    assert result.is_error and "prohibited" in result.content[0].text


def test_mcp_over_stdio_subprocess(indexed: Workspace) -> None:
    async def call() -> tuple[list[str], dict[str, Any]]:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "dossier", "--workspace", str(indexed.root), "mcp", "serve"],
        )
        async with Client(params) as client:
            names = [tool.name for tool in (await client.list_tools()).tools]
            status = _structured(await client.call_tool("status", {}))
            return names, status

    names, status = asyncio.run(call())
    assert "search" in names and status["workspace"] == "Acme"


def test_navigation_files(indexed: Workspace) -> None:
    agents = (indexed.root / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.startswith(MARKER) and "dsr read <doc> --lines a-b" in agents
    assert "processing is `approved`" in agents
    assert "[Tree](index/TREE.md)" in (indexed.root / "llms.txt").read_text(encoding="utf-8")
    (indexed.root / "AGENTS.md").write_text("# Our own notes\n", encoding="utf-8")
    written = write_navigation(open_workspace(indexed.root))
    assert written == {"AGENTS.md": False, "llms.txt": True}
    assert (indexed.root / "AGENTS.md").read_text(encoding="utf-8") == "# Our own notes\n"


def test_mcp_install_commands(workspace: Workspace) -> None:
    ws = ["--workspace", str(workspace.root)]
    result = runner.invoke(app, ["--json", *ws, "mcp", "install"])
    command = json.loads(result.stdout)["command"]
    assert command[:5] == ["claude", "mcp", "add", "--scope", "local"]
    assert command[5] == "dossier-acme" and command[-2:] == ["mcp", "serve"]
    snippet = json.loads(
        runner.invoke(app, ["--json", *ws, "mcp", "install", "--client", "json"]).stdout
    )
    assert "dossier-acme" in snippet["mcpServers"]


def test_skill_is_packaged() -> None:
    text = (skill_source() / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: dossier\n")
