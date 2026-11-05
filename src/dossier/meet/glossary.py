"""Workspace glossary for transcription.

``glossary.toml``::

    terms = ["Lakehouse", "DataOps", "Falcon"]      # spelled as they should appear

    [replacements]                                  # frequent mishearings -> correct form
    "data ops" = "DataOps"
    "lake house" = "Lakehouse"

Terms bias the recognizer through Whisper's initial prompt; replacements fix
the text afterwards (case-insensitive, whole words). The glossary also drives
``dsr meet eval``.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dossier.core.errors import ConfigError

_PROMPT_LIMIT = 800  # characters; Whisper's prompt window is small


@dataclass
class Glossary:
    terms: list[str] = field(default_factory=list)
    replacements: dict[str, str] = field(default_factory=dict)
    _pattern: re.Pattern[str] | None = field(default=None, init=False, repr=False)

    @classmethod
    def load(cls, path: Path | None) -> Glossary:
        if path is None or not path.is_file():
            return cls()
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"glossary {path} is not valid TOML: {exc}", path=path) from exc
        terms = [str(t).strip() for t in data.get("terms", []) if str(t).strip()]
        replacements = {
            str(k).strip().lower(): str(v) for k, v in dict(data.get("replacements", {})).items()
        }
        return cls(terms=terms, replacements=replacements)

    def prompt(self) -> str | None:
        if not self.terms:
            return None
        text = "Glossary: " + ", ".join(self.terms) + "."
        return text[:_PROMPT_LIMIT]

    def apply(self, text: str) -> str:
        if not self.replacements:
            return text
        if self._pattern is None:
            keys = sorted(self.replacements, key=len, reverse=True)
            self._pattern = re.compile(
                r"(?<!\w)(" + "|".join(re.escape(k) for k in keys) + r")(?!\w)", re.IGNORECASE
            )
        return self._pattern.sub(lambda m: self.replacements[m.group(0).lower()], text)
