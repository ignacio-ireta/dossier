"""MCP tools for meetings: list, transcript slices, action items."""

from __future__ import annotations

from typing import Any

from dossier.agent.mcp_server import EXTENSIONS, WorkspaceGetter
from dossier.core.io import read_json
from dossier.core.policy import require_not_prohibited
from dossier.meet.models import Turn, clock, parse_clock
from dossier.meet.pipeline import list_meetings, load_record, resolve_meeting, speakers_summary


def register(server: Any, get_workspace: WorkspaceGetter) -> None:
    from mcp.types import ToolAnnotations

    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @server.tool(annotations=read_only)
    def meetings(limit: int = 20, contains: str | None = None) -> dict[str, Any]:
        """Meetings, newest first: id, title, date, duration, speakers, summary status."""
        workspace = get_workspace()
        require_not_prohibited(workspace.config, "meetings")
        records = list_meetings(workspace)
        if contains:
            records = [r for r in records if contains.lower() in r.title.lower()]
        return {
            "meetings": [
                {
                    "id": r.id,
                    "title": r.title,
                    "date": r.date,
                    "duration": clock(r.duration_s or 0),
                    "speakers": speakers_summary(r),
                    "summary_status": r.summary_status,
                    "summary": f"meetings/{r.id}/summary.md",
                }
                for r in records[: max(1, min(limit, 200))]
            ]
        }

    @server.tool(annotations=read_only)
    def transcript(
        meeting: str, start: str | None = None, end: str | None = None
    ) -> dict[str, Any]:
        """Transcript turns of one meeting, optionally between two times ("mm:ss" or "h:mm:ss").

        Speaker names marked "likely" are unconfirmed suggestions.
        """
        workspace = get_workspace()
        require_not_prohibited(workspace.config, "transcript")
        folder = resolve_meeting(workspace, meeting)
        record = load_record(folder)
        turns = [Turn(**t) for t in read_json(folder / "machine" / "turns.json")]
        low = parse_clock(start) if start else None
        high = parse_clock(end) if end else None
        selected = [
            t for t in turns if (low is None or t.end >= low) and (high is None or t.start <= high)
        ]
        return {
            "meeting": record.id,
            "title": record.title,
            "turns": [
                {
                    "at": clock(t.start),
                    "speaker": record.speakers[t.speaker].display()
                    if t.speaker in record.speakers
                    else t.speaker,
                    "text": t.text,
                }
                for t in selected[:400]
            ],
            "truncated": len(selected) > 400,
        }

    @server.tool(annotations=read_only)
    def actions(meeting: str | None = None, owner: str | None = None) -> dict[str, Any]:
        """Action items with owner, due date (UNKNOWN unless stated) and the time they were said."""
        workspace = get_workspace()
        require_not_prohibited(workspace.config, "actions")
        records = (
            [load_record(resolve_meeting(workspace, meeting))]
            if meeting
            else list_meetings(workspace)
        )
        items = []
        for record in records:
            path = workspace.paths.meetings / record.id / "actions.json"
            if not path.is_file():
                continue
            for action in read_json(path):
                if owner and owner.lower() not in str(action.get("owner", "")).lower():
                    continue
                items.append(
                    {"meeting": record.id, "title": record.title, "date": record.date, **action}
                )
        return {"actions": items}


EXTENSIONS.append(register)
