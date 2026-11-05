"""Workspace glossary for transcription.

``glossary.toml``::

    terms = ["Lakehouse", "DataOps", "Falcon"]      # spelled as they should appear

    [replacements]                                  # frequent mishearings -> correct form
    "data ops" = "DataOps"
    "lake house" = "Lakehouse"

    [es]                                            # only for meetings in Spanish
    terms = ["facturación electrónica"]
    [es.replacements]
    "cuberneti" = "Kubernetes"

Terms bias the recognizer through Whisper's initial prompt; replacements fix
the text afterwards (case-insensitive, whole words, and they may span several
words). A table named after a language code (``[es]``, ``[en]``...) adds terms
and replacements for meetings in that language. The glossary also drives
``dsr meet eval``.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dossier.core.errors import ConfigError
from dossier.core.languages import LANGUAGES, language_code
from dossier.meet.models import Word

_PROMPT_LIMIT = 800  # characters; Whisper's prompt window is small
# Whisper reads the prompt as text said just before the recording, so its label
# is in the meeting's language where we know the word.
_PROMPT_LABEL = {
    "en": "Glossary",
    "es": "Glosario",
    "pt": "Glossário",
    "fr": "Glossaire",
    "it": "Glossario",
    "de": "Glossar",
    "nl": "Woordenlijst",
    "ca": "Glossari",
}


def _key(text: str) -> str:
    return " ".join(text.lower().split())


def _terms(data: dict[str, object]) -> list[str]:
    raw = data.get("terms", [])
    if not isinstance(raw, list):
        raise ConfigError('glossary "terms" must be a list of strings')
    return [str(t).strip() for t in raw if str(t).strip()]


def _replacements(data: dict[str, object]) -> dict[str, str]:
    raw = data.get("replacements", {})
    if not isinstance(raw, dict):
        raise ConfigError('glossary "replacements" must be a table of "heard" = "correct"')
    return {_key(str(k)): str(v) for k, v in raw.items() if _key(str(k))}


@dataclass
class Glossary:
    terms: list[str] = field(default_factory=list)
    replacements: dict[str, str] = field(default_factory=dict)
    by_language: dict[str, Glossary] = field(default_factory=dict)
    _pattern: re.Pattern[str] | None = field(default=None, init=False, repr=False)

    @classmethod
    def load(cls, path: Path | None) -> Glossary:
        if path is None or not path.is_file():
            return cls()
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"glossary {path} is not valid TOML: {exc}", path=path) from exc
        by_language: dict[str, Glossary] = {}
        for name, value in data.items():
            if name in {"terms", "replacements"}:
                continue
            code = language_code(name)
            if not isinstance(value, dict) or code is None or code not in LANGUAGES:
                raise ConfigError(
                    f"glossary {path}: unknown table [{name}]; use terms, replacements, "
                    "or a language code such as [es]",
                    path=path,
                )
            by_language[code] = cls(terms=_terms(value), replacements=_replacements(value))
        return cls(terms=_terms(data), replacements=_replacements(data), by_language=by_language)

    def for_language(self, language: str | None) -> Glossary:
        """The shared entries plus those for ``language`` (which win on conflicts)."""
        extra = self.by_language.get(language or "")
        if extra is None:
            return Glossary(terms=list(self.terms), replacements=dict(self.replacements))
        return Glossary(
            terms=list(dict.fromkeys([*extra.terms, *self.terms])),
            replacements={**self.replacements, **extra.replacements},
        )

    def prompt(self, language: str | None = None) -> str | None:
        if not self.terms:
            return None
        terms = ", ".join(self.terms) + "."
        label = _PROMPT_LABEL.get(language or "en")
        text = f"{label}: {terms}" if label else terms
        return text[:_PROMPT_LIMIT]

    def _compiled(self) -> re.Pattern[str]:
        if self._pattern is None:
            keys = sorted(self.replacements, key=len, reverse=True)
            alternatives = (r"\s+".join(re.escape(part) for part in k.split()) for k in keys)
            self._pattern = re.compile(
                r"(?<!\w)(" + "|".join(alternatives) + r")(?!\w)", re.IGNORECASE
            )
        return self._pattern

    def apply(self, text: str) -> str:
        if not self.replacements:
            return text
        return self._compiled().sub(lambda m: self.replacements[_key(m.group(0))], text)

    def apply_words(self, words: list[Word]) -> list[Word]:
        """Apply replacements across recognized words, keeping their timings.

        A replacement that spans several words ("data ops" -> "DataOps") turns
        them into one word that starts with the first and ends with the last.
        """
        if not self.replacements or not words:
            return words
        starts: list[int] = []
        position = 0
        for word in words:
            starts.append(position)
            position += len(word.text) + 1
        joined = " ".join(word.text for word in words)
        spans: list[tuple[int, int]] = []  # word index ranges touched by a match
        for match in self._compiled().finditer(joined):
            first = _word_at(starts, match.start())
            last = _word_at(starts, match.end() - 1)
            if spans and first <= spans[-1][1]:
                spans[-1] = (spans[-1][0], max(spans[-1][1], last))
            else:
                spans.append((first, last))
        if not spans:
            return words
        result: list[Word] = []
        index = 0
        for first, last in spans:
            result.extend(words[index:first])
            group = words[first : last + 1]
            confidences = [w.confidence for w in group if w.confidence is not None]
            result.append(
                group[0].model_copy(
                    update={
                        "end": group[-1].end,
                        "text": self.apply(" ".join(w.text for w in group)),
                        "confidence": min(confidences) if confidences else None,
                    }
                )
            )
            index = last + 1
        result.extend(words[index:])
        return result


def _word_at(starts: list[int], offset: int) -> int:
    """Index of the word whose text contains character ``offset`` of the joined text."""
    low, high = 0, len(starts) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if starts[middle] <= offset:
            low = middle
        else:
            high = middle - 1
    return low
