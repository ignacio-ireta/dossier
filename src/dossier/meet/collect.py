"""Meetings in the rest of dossier: the sync stage, the index, and pre-meeting briefs.

- The ``meetings`` sync stage transcribes recordings found in source folders
  (audio/video files the converter routes here), once per content hash.
- The index collector makes transcripts and summaries searchable
  (``--kind meeting``) and gives them cards like any document.
- ``build_brief`` assembles a preparation note from a meeting's summary, its
  deck outline and related documents, without a model call.
"""

from __future__ import annotations

import logging
from typing import Any

from dossier.convert.discovery import discover
from dossier.core.errors import DossierError
from dossier.core.io import atomic_write_json, atomic_write_text, read_json, sha256_text
from dossier.core.workspace import Workspace
from dossier.index.pipeline import Indexable
from dossier.meet.backends import meet_extra_available
from dossier.meet.models import clock
from dossier.meet.pipeline import (
    MeetOptions,
    list_meetings,
    load_record,
    process_recording,
    summary_from_json,
)
from dossier.meet.render import confirmed_names

log = logging.getLogger("dossier.meet")


def meeting_documents(workspace: Workspace) -> list[Indexable]:
    items: list[Indexable] = []
    for record in list_meetings(workspace):
        folder = workspace.paths.meetings / record.id
        for name, doc_type, label in (
            ("summary.md", "meeting-summary", "summary"),
            ("transcript.md", "transcript", "transcript"),
        ):
            path = folder / name
            if path.is_file():
                items.append(
                    Indexable(
                        doc_key=f"meetings/{record.id}/{label}",
                        md_path=f"meetings/{record.id}/{name}",
                        output_sha256=sha256_text(path.read_text(encoding="utf-8")),
                        title=f"{record.title} ({label}, {record.date})",
                        doc_type=doc_type,
                        kind="meeting",
                    )
                )
    return items


def _state_path(workspace: Workspace):  # type: ignore[no-untyped-def]
    return workspace.paths.state / "state" / "meet" / "media.json"


def meetings_stage(workspace: Workspace, *, dry_run: bool, scheduled: bool) -> dict[str, Any]:
    settings = workspace.config.meet
    state_path = _state_path(workspace)
    state: dict[str, dict[str, Any]] = read_json(state_path) if state_path.is_file() else {}
    pending = []
    for source in workspace.config.sources:
        root = workspace.paths.resolve(source.path)
        if not root.is_dir():
            continue
        for media in discover(root, source, max_bytes=1 << 62).media:
            key = f"{source.name}/{media.relpath}"
            known = state.get(key)
            if (
                known
                and known.get("size") == media.size
                and known.get("mtime_ns") == media.mtime_ns
            ):
                continue
            pending.append((key, media))
    counts: dict[str, int] = {"recordings_new": len(pending), "transcribed": 0, "failed": 0}
    notes: list[str] = []
    if not pending or dry_run:
        return {"counts": counts, "notes": notes}
    if not settings.auto_transcribe:
        notes.append("auto_transcribe is off; use `dsr meet add`")
        return {"counts": counts, "notes": notes}
    if not meet_extra_available():
        notes.append("recordings found; install the meet extra to transcribe them")
        return {"counts": counts, "notes": notes}
    attention: list[str] = []
    for key, media in pending:
        try:
            record = process_recording(workspace, media.path, MeetOptions(scheduled=scheduled))
            if record.review_status == "pending":
                unnamed = sum(1 for s in record.speakers.values() if s.status != "confirmed")
                counts["to_identify"] = counts.get("to_identify", 0) + 1
                notes.append(
                    f"meeting {record.id}: {unnamed} speaker(s) to name; "
                    f"run `dsr meet identify {record.id}`"
                )
            state[key] = {
                "size": media.size,
                "mtime_ns": media.mtime_ns,
                "meeting": record.id,
                "status": "ok",
            }
            counts["transcribed"] += 1
        except DossierError as exc:
            state[key] = {
                "size": media.size,
                "mtime_ns": media.mtime_ns,
                "status": "failed",
                "error": exc.code,
            }
            counts["failed"] += 1
            attention.append(f"meeting {key}: {exc.message}")
        atomic_write_json(state_path, state)
    return {"counts": counts, "notes": notes, "attention": attention}


def build_brief(workspace: Workspace, meeting_id: str, *, related: int = 5) -> str:
    """A preparation note: summary items, deck outline, related documents."""
    from dossier.index.chunking import outline
    from dossier.index.search import SearchFilters, search

    folder = workspace.paths.meetings / meeting_id
    record = load_record(folder)
    summary_path = folder / "machine" / "summary.json"
    summary = summary_from_json(read_json(summary_path)) if summary_path.is_file() else None

    def names(text: str) -> str:
        return confirmed_names(record, text)

    lines = [f"# Brief: {record.title}", "", f"{record.date} · {clock(record.duration_s or 0)}", ""]
    if summary:
        lines += ["## What was discussed", "", names(summary.overview), ""]
        if summary.actions:
            lines += ["## Open actions", ""]
            lines += [
                f"- {names(a.text)} (owner: {names(a.owner)}, due: {a.due}) [{a.stamp()}]"
                for a in summary.actions
            ]
            lines.append("")
        if summary.questions:
            lines += ["## Open questions", ""]
            lines += [f"- {names(q.text)} [{q.stamp()}]" for q in summary.questions]
            lines.append("")
    deck = folder / "deck.md"
    if deck.is_file():
        slides = [e.text for e in outline(deck.read_text(encoding="utf-8")) if e.level == 2]
        if slides:
            lines += ["## Slides", ""] + [f"- {s}" for s in slides] + [""]
    query = " ".join([record.title, *(summary.topics if summary else [])])
    try:
        hits, _ = search(
            workspace, query, limit=related, filters=SearchFilters(kind="document"), per_doc=1
        )
    except DossierError:
        hits = []
    if hits:
        lines += ["## Related documents", ""]
        lines += [f"- {h.title} (`{h.doc}`): {h.snippet[:140]}" for h in hits]
        lines.append("")
    text = "\n".join(lines).rstrip() + "\n"
    atomic_write_text(folder / "brief.md", text)
    return text
