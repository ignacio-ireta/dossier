"""End-to-end check with real tools and models (not collected by pytest).

    uv run python tests/e2e.py [--workdir DIR] [--dsr PATH] [--skip-meetings] [--qmd]

Builds an invented project (documents plus a two-voice meeting recording),
then drives the real ``dsr`` command like a user would: init, approve, sync
(convert, meetings, index), search for every planted fact, check the meeting,
render DOCX/PPTX/PDF with a Mermaid diagram, and talk to the MCP server over
stdio. Needs Ollama with the default models, ffmpeg, pandoc, typst and mmdc;
meetings also need the meet extra and the pyannote model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from dossier.eval.synthetic import MEETING_SCRIPT, build_corpus, build_meeting

REPORT = """---
title: Falcon Platform Assessment
author: [Platform Team]
version: "1.0"
---

# Summary

The platform consolidates reporting for the operations teams.

## Flow

```mermaid
%% caption: Data flow
flowchart LR
  A[Sources] --> B[Ingestion] --> C[Dashboards]
```

| Workstream | Cost |
|---|---|
| Ingestion | 340000 |
"""


class Checks:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.rows if not ok)


def dsr(command: list[str], workspace: Path, *args: str) -> tuple[int, Any]:
    completed = subprocess.run(
        [*command, "--json", "--workspace", str(workspace), *args],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    try:
        data = json.loads(completed.stdout) if completed.stdout.strip() else None
    except json.JSONDecodeError:
        data = completed.stdout
    return completed.returncode, data


async def mcp_check(command: list[str], workspace: Path) -> tuple[list[str], dict[str, Any]]:
    from mcp import Client, StdioServerParameters

    params = StdioServerParameters(
        command=command[0], args=[*command[1:], "--workspace", str(workspace), "mcp", "serve"]
    )
    async with Client(params) as client:
        names = sorted(t.name for t in (await client.list_tools()).tools)
        result = await client.call_tool("search", {"query": "hosting option decision"})
        return names, dict(result.structured_content or {})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--dsr", default=None, help="dsr executable (default: on PATH)")
    parser.add_argument("--skip-meetings", action="store_true")
    parser.add_argument("--qmd", action="store_true", help="also check the qmd backend")
    args = parser.parse_args()
    command = (
        [args.dsr]
        if args.dsr
        else ([shutil.which("dsr")] if shutil.which("dsr") else [sys.executable, "-m", "dossier"])
    )
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="dossier-e2e-"))
    workspace = workdir / "falcon"
    if workspace.exists():
        shutil.rmtree(workspace)
    checks = Checks()
    started = time.monotonic()

    code, data = dsr(command, workspace.parent, "init", str(workspace), "--name", "Falcon")
    checks.add("init", code == 0, str(workspace))
    facts = build_corpus(workspace / "sources")
    if not args.skip_meetings:
        build_meeting(
            workspace / "sources" / "calls" / "2026-11-02 Rollout review.m4a",
            ("Samantha", "Daniel"),
        )
    code, _ = dsr(command, workspace, "sync")
    checks.add("sync blocked before approval", code == 78, f"exit {code}")
    code, _ = dsr(command, workspace, "policy", "approve", "--yes", "--note", "synthetic e2e")
    checks.add("policy approve", code == 0)

    code, data = dsr(command, workspace, "sync")
    stages = data.get("stages", {}) if isinstance(data, dict) else {}
    converted = stages.get("convert", {}).get("main", {}).get("counts", {})
    index = stages.get("index", {}).get("counts", {})
    checks.add(
        "sync",
        code == 0,
        f"exit {code}; converted {converted.get('added')}, "
        f"cards {index.get('cards_created')}, degraded {index.get('cards_degraded')}",
    )
    checks.add("cards made by the model", index.get("cards_degraded", 1) == 0)
    for name in ("INDEX.md", "TREE.md", "index.json"):
        checks.add(f"index/{name}", (workspace / "index" / name).is_file())
    checks.add("AGENTS.md", (workspace / "AGENTS.md").is_file())

    for fact in facts:
        code, data = dsr(command, workspace, "search", fact.question, "-k", "5")
        docs = [hit["doc"] for hit in data.get("results", [])] if isinstance(data, dict) else []
        target = f"main/{fact.file}"
        rank = docs.index(target) + 1 if target in docs else None
        checks.add(f"search: {fact.file}", rank is not None, f"rank {rank}")

    if not args.skip_meetings:
        meetings = stages.get("meetings", {}).get("counts", {})
        checks.add(
            "meeting transcribed during sync",
            meetings.get("transcribed") == 1,
            json.dumps(meetings),
        )
        code, data = dsr(command, workspace, "meet", "list")
        meeting = data[0] if isinstance(data, list) and data else {}
        checks.add(
            "two speakers",
            len(meeting.get("speakers", [])) == 2,
            str([s["label"] for s in meeting.get("speakers", [])]),
        )
        actions_path = workspace / "meetings" / meeting.get("id", "?") / "actions.json"
        actions = json.loads(actions_path.read_text()) if actions_path.is_file() else []
        duration = len(MEETING_SCRIPT) * 6
        checks.add(
            "action items with valid times",
            bool(actions) and all(0 <= a["at_seconds"] <= duration for a in actions),
            "; ".join(f"{a['task']} ({a['owner']}, {a['due']}, {a['at']})" for a in actions),
        )
        code, data = dsr(
            command, workspace, "search", "who approves the storage budget", "--kind", "meeting"
        )
        hits = data.get("results", []) if isinstance(data, dict) else []
        checks.add("meeting searchable", bool(hits), hits[0]["doc"] if hits else "")

    report = workspace / "out" / "assessment.md"
    report.write_text(REPORT, encoding="utf-8")
    for fmt in ("docx", "pptx", "pdf"):
        code, data = dsr(command, workspace, "docs", "render", str(report), "--to", fmt)
        output = Path(data["output"]) if isinstance(data, dict) and "output" in data else None
        checks.add(
            f"render {fmt}",
            code == 0 and output is not None and output.is_file(),
            f"diagrams {data.get('diagrams') if isinstance(data, dict) else '?'}",
        )

    names, result = asyncio.run(mcp_check(command, workspace))
    checks.add(
        "mcp tools", {"search", "read", "meetings", "templates"} <= set(names), ", ".join(names)
    )
    first = (result.get("results") or [{}])[0].get("doc")
    checks.add("mcp search", first == "main/mail/steering-decision.eml", str(first))

    if args.qmd:
        code, _ = dsr(command, workspace, "qmd", "sync")
        checks.add("qmd sync", code == 0)
        code, data = dsr(
            command, workspace, "search", "Which risk was rated highest?", "--backend", "qmd"
        )
        docs = [h["doc"] for h in data.get("results", [])] if isinstance(data, dict) else []
        checks.add("qmd search", "main/reports/risk-review.pdf" in docs[:5], str(docs[:3]))

    elapsed = round(time.monotonic() - started)
    print(
        f"\n{len(checks.rows) - checks.failed}/{len(checks.rows)} checks passed in {elapsed}s; "
        f"workspace: {workspace}"
    )
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
