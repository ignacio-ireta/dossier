"""Words -> speakers -> turns, and speaker suggestions from voiceprints."""

from __future__ import annotations

import math
from collections import defaultdict

from dossier.meet.models import DiarSegment, SpeakerInfo, Turn, Word

UNKNOWN = "SPEAKER_00"


def speaker_for(word: Word, segments: list[DiarSegment]) -> str:
    if not segments:
        return UNKNOWN
    midpoint = (word.start + word.end) / 2
    for segment in segments:
        if segment.start <= midpoint <= segment.end:
            return segment.speaker
    best, overlap = None, 0.0
    for segment in segments:
        shared = max(0.0, min(word.end, segment.end) - max(word.start, segment.start))
        if shared > overlap:
            best, overlap = segment, shared
    if best is not None:
        return best.speaker
    nearest = min(segments, key=lambda s: min(abs(s.start - midpoint), abs(s.end - midpoint)))
    return nearest.speaker


def assign(words: list[Word], segments: list[DiarSegment]) -> list[Word]:
    return [w.model_copy(update={"speaker": speaker_for(w, segments)}) for w in words]


def to_turns(words: list[Word], *, max_gap: float = 1.5, max_turn: float = 45.0) -> list[Turn]:
    turns: list[Turn] = []
    current: list[Word] = []
    for word in words:
        if current:
            same = word.speaker == current[0].speaker
            gap = word.start - current[-1].end
            long = word.end - current[0].start > max_turn
            if not same or gap > max_gap or long:
                turns.append(_turn(current))
                current = []
        current.append(word)
    if current:
        turns.append(_turn(current))
    return turns


def _turn(words: list[Word]) -> Turn:
    return Turn(
        start=words[0].start,
        end=words[-1].end,
        speaker=words[0].speaker or UNKNOWN,
        text=" ".join(w.text for w in words),
    )


def speaker_table(segments: list[DiarSegment], turns: list[Turn]) -> dict[str, SpeakerInfo]:
    seconds: dict[str, float] = defaultdict(float)
    if segments:
        for segment in segments:
            seconds[segment.speaker] += max(0.0, segment.end - segment.start)
    else:
        for turn in turns:
            seconds[turn.speaker] += max(0.0, turn.end - turn.start)
    return {
        label: SpeakerInfo(label=label, seconds=round(total, 1))
        for label, total in sorted(seconds.items())
    }


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def suggest_names(
    speakers: dict[str, SpeakerInfo],
    embeddings: dict[str, list[float]],
    voiceprints: dict[str, list[float]],
    *,
    threshold: float,
    roster: list[str] | None = None,
) -> dict[str, SpeakerInfo]:
    """Best-scoring person per speaker, each person used at most once (greedy)."""
    candidates = {
        name: vector for name, vector in voiceprints.items() if not roster or name in roster
    }
    scores = sorted(
        (
            (cosine(embeddings[label], vector), label, name)
            for label in speakers
            if label in embeddings
            for name, vector in candidates.items()
        ),
        reverse=True,
    )
    taken_labels: set[str] = set()
    taken_names: set[str] = set()
    updated = {label: info.model_copy() for label, info in speakers.items()}
    for score, label, name in scores:
        if score < threshold or label in taken_labels or name in taken_names:
            continue
        if updated[label].status == "confirmed":
            taken_labels.add(label)
            continue
        updated[label] = updated[label].model_copy(
            update={"name": name, "status": "suggested", "confidence": round(score, 2)}
        )
        taken_labels.add(label)
        taken_names.add(name)
    return updated
