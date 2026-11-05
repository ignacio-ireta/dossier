"""Meeting summaries and action items with a small model.

The transcript is cut into timestamped windows that fit the ``meet`` role's
budget. Each window gets one extraction call returning a short summary plus the
decisions, actions and open questions *stated in that window*, each with the
timestamp where it is said. A final call writes the overview from the window
summaries. Decisions, actions and questions are merged without a model (exact
and near-duplicate removal).

Every item's timestamp is checked against the transcript: it must fall inside
the window and on an actual turn. Items that fail are dropped and counted.
Owners and due dates are kept only as stated; anything else becomes UNKNOWN.

Notes are written in the meeting's language: quoting the meeting in its own
language keeps stated owners and due dates verifiable against the transcript.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from dossier.core.config import WorkspaceConfig
from dossier.core.languages import AUTO, LANGUAGES, language_name
from dossier.llm.budget import pack, truncate_to_tokens
from dossier.llm.router import RoleClient
from dossier.llm.structured import Outcome, generate_structured
from dossier.meet.models import SpeakerInfo, Turn, clock, parse_clock

_OVERHEAD = 900
log = logging.getLogger("dossier.meet")
_UNKNOWN = "UNKNOWN"


class _Item(BaseModel):
    text: str = Field(max_length=300)
    at: str = Field(description="mm:ss timestamp where it is said")


class _Action(BaseModel):
    task: str = Field(max_length=300)
    owner: str = Field(default=_UNKNOWN, max_length=80)
    due: str = Field(default=_UNKNOWN, max_length=60)
    at: str


class _Window(BaseModel):
    summary: str = Field(max_length=700)
    decisions: list[_Item] = Field(default_factory=list, max_length=6)
    actions: list[_Action] = Field(default_factory=list, max_length=8)
    questions: list[_Item] = Field(default_factory=list, max_length=5)
    topics: list[str] = Field(default_factory=list, max_length=5)


class _Overview(BaseModel):
    overview: str = Field(max_length=1200)
    topics: list[str] = Field(default_factory=list, max_length=8)


@dataclass
class Item:
    text: str
    at: float
    owner: str = _UNKNOWN
    due: str = _UNKNOWN

    def stamp(self) -> str:
        return clock(self.at)


@dataclass
class MeetingSummary:
    overview: str = ""
    topics: list[str] = field(default_factory=list)
    decisions: list[Item] = field(default_factory=list)
    actions: list[Item] = field(default_factory=list)
    questions: list[Item] = field(default_factory=list)
    status: str = "ok"
    windows: int = 0
    degraded_windows: int = 0
    dropped_items: int = 0
    calls: int = 0


def transcript_lines(turns: list[Turn], speakers: dict[str, SpeakerInfo]) -> list[str]:
    """Lines the model sees. Unconfirmed names never reach the model."""
    lines = []
    for turn in turns:
        info = speakers.get(turn.speaker)
        who = info.name if info and info.status == "confirmed" and info.name else turn.speaker
        lines.append(f"[{clock(turn.start)}] {who}: {turn.text}")
    return lines


def _valid_time(value: str, turns: list[Turn], start: float, end: float) -> float | None:
    seconds = parse_clock(value)
    if seconds is not None and start - 1 <= seconds <= end + 1:
        for turn in turns:
            if turn.start - 1 <= seconds <= turn.end + 1:
                return seconds
    log.info("dropped an item with an unverifiable time %r", value[:16])
    return None


def _clean_field(value: str, stated_in: str) -> str:
    """Keep an owner/due value only if it literally occurs in the window text."""
    value = value.strip()
    if not value or value.upper() == _UNKNOWN:
        return _UNKNOWN
    return value if value.lower() in stated_in.lower() else _UNKNOWN


def _norm(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def _dedupe(items: list[Item]) -> list[Item]:
    kept: list[Item] = []
    for item in sorted(items, key=lambda i: i.at):
        words = _norm(item.text)
        if any(
            len(words & _norm(k.text)) / max(1, len(words | _norm(k.text))) > 0.75 for k in kept
        ):
            continue
        kept.append(item)
    return kept


def summarize(
    client: RoleClient | None,
    config: WorkspaceConfig,
    *,
    title: str,
    turns: list[Turn],
    speakers: dict[str, SpeakerInfo],
    context: str = "",
    language: str | None = None,
) -> MeetingSummary:
    result = MeetingSummary()
    if not turns:
        result.overview = "No speech was recognized in this recording."
        return result
    if client is None:
        result.status = "degraded"
        result.overview = "Summary unavailable: the meeting model was not reachable."
        return result
    lines = transcript_lines(turns, speakers)
    budget = max(500, client.input_budget_tokens - _OVERHEAD)
    windows = pack(lines, budget, separator="\n")
    result.windows = len(windows)
    brief = config.workspace.brief.strip()
    write_in = (
        f" Write in {language_name(language)}, the language of the meeting."
        if language and language != AUTO and language in LANGUAGES
        else " Write in the language of the meeting."
    )
    system = (
        f'You take notes on meetings for the workspace "{config.workspace.name}".'
        + (f" Workspace context: {brief}" if brief else "")
        + " Use only what is said in the transcript. Every item needs the [mm:ss] time where it "
        "is said. owner and due: only if a person or date is explicitly stated for that action, "
        'otherwise "UNKNOWN". Do not guess. Speaker labels like SPEAKER_01 are not names.'
        + write_in
    )
    window_summaries: list[str] = []
    for number, window in enumerate(windows, start=1):
        text = "\n".join(lines[i] for i in window)
        window_turns = [turns[i] for i in window]
        start, end = window_turns[0].start, window_turns[-1].end
        reply = generate_structured(
            client,
            system=system,
            user=(
                f'Meeting "{title}", part {number} of {len(windows)} '
                f"({clock(start)}-{clock(end)}):\n{text}\n\nReturn: summary (2-3 sentences), "
                "decisions (agreed outcomes), actions (tasks someone will do: task, owner, due, "
                "at), questions (open questions), topics (up to 4 keywords)."
            ),
            schema=_Window,
            fallback=lambda: _Window(summary=""),
        )
        result.calls += reply.attempts
        if reply.outcome is Outcome.DEGRADED or not reply.value.summary:
            result.degraded_windows += 1
            continue
        notes = reply.value
        window_summaries.append(f"[{clock(start)}-{clock(end)}] {notes.summary}")
        result.topics.extend(t.lower().strip() for t in notes.topics if t.strip())
        for decision in notes.decisions:
            at = _valid_time(decision.at, window_turns, start, end)
            if at is None:
                result.dropped_items += 1
            else:
                result.decisions.append(Item(decision.text.strip(), at))
        for question in notes.questions:
            at = _valid_time(question.at, window_turns, start, end)
            if at is None:
                result.dropped_items += 1
            else:
                result.questions.append(Item(question.text.strip(), at))
        for action in notes.actions:
            at = _valid_time(action.at, window_turns, start, end)
            if at is None:
                result.dropped_items += 1
                continue
            result.actions.append(
                Item(
                    action.task.strip(),
                    at,
                    _clean_field(action.owner, text),
                    _clean_field(action.due, text),
                )
            )
    result.decisions = _dedupe(result.decisions)
    result.actions = _dedupe(result.actions)
    result.questions = _dedupe(result.questions)
    result.topics = list(dict.fromkeys(result.topics))[:8]
    if result.degraded_windows:
        result.status = "degraded"
    if not window_summaries:
        result.overview = "Summary unavailable: the model gave no usable notes."
        result.status = "degraded"
        return result
    notes_text = truncate_to_tokens("\n".join(window_summaries), budget)
    overview = generate_structured(
        client,
        system=system,
        user=(
            f'Notes on consecutive parts of the meeting "{title}":\n{notes_text}\n'
            + (
                f"\nMaterial shown in the meeting:\n{truncate_to_tokens(context, 600)}\n"
                if context
                else ""
            )
            + "\nReturn: overview (3-5 sentences: purpose, main points, outcome), topics "
            "(up to 6 keywords)."
        ),
        schema=_Overview,
        fallback=lambda: _Overview(overview=" ".join(window_summaries)[:1200]),
    )
    result.calls += overview.attempts
    result.overview = overview.value.overview.strip()
    if overview.outcome is Outcome.DEGRADED:
        result.status = "degraded"
    merged = [t.lower().strip() for t in overview.value.topics] + result.topics
    result.topics = list(dict.fromkeys(t for t in merged if t))[:8]
    return result
