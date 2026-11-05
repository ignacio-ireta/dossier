"""``dsr meet eval``: score a transcript for consultant-grade quality.

Raw word error rate says little about whether a transcript is usable in client
work. This score rewards correctly spelled glossary terms and penalizes known
mishearings (the glossary's replacement keys) and hallucination loops (the same
word repeated many times, a typical Whisper failure on silence or noise):

    score = 100 * term_recall - 5 * bad_forms - 0.5 * loop_words
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field

from dossier.meet.glossary import Glossary

LOOP_RUN = 6


def _norm(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped.lower())


@dataclass
class TranscriptScore:
    score: float
    term_recall: float
    terms_found: list[str] = field(default_factory=list)
    terms_missing: list[str] = field(default_factory=list)
    bad_forms: list[str] = field(default_factory=list)
    loops: list[tuple[str, int]] = field(default_factory=list)
    loop_words: int = 0

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def loops(text: str, min_run: int = LOOP_RUN) -> list[tuple[str, int]]:
    words = re.findall(r"\w+", text.lower())
    found: list[tuple[str, int]] = []
    index = 0
    while index < len(words):
        run = 1
        while index + run < len(words) and words[index + run] == words[index]:
            run += 1
        if run >= min_run:
            found.append((words[index], run))
        index += run
    return found


def score(text: str, glossary: Glossary) -> TranscriptScore:
    normalized = _norm(text)
    found = [t for t in glossary.terms if _norm(t) in normalized]
    missing = [t for t in glossary.terms if t not in found]
    recall = len(found) / len(glossary.terms) if glossary.terms else 1.0
    bad = [
        wrong
        for wrong, right in glossary.replacements.items()
        if _norm(wrong) != _norm(right)
        and re.search(rf"(?<!\w){re.escape(_norm(wrong))}(?!\w)", normalized)
    ]
    repeated = loops(text)
    loop_words = sum(run for _, run in repeated)
    return TranscriptScore(
        score=round(100 * recall - 5 * len(bad) - 0.5 * loop_words, 2),
        term_recall=round(recall, 3),
        terms_found=found,
        terms_missing=missing,
        bad_forms=bad,
        loops=repeated,
        loop_words=loop_words,
    )
