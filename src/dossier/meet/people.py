"""Opt-in voiceprints for speaker suggestions.

Voiceprints are biometric data. They exist only when the workspace sets
``[meet] speaker_voiceprints = true``, are stored only in this workspace
(``people/voiceprints.json``), and a person's voiceprint is created or updated
only after their consent is recorded (``dsr people consent``). ``dsr people
forget`` deletes everything stored about a person.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from dossier.core.errors import DossierError
from dossier.core.io import atomic_write_text
from dossier.core.paths import WorkspacePaths


class Person(BaseModel):
    name: str
    consent_at: str | None = None
    consent_note: str = ""
    centroid: list[float] = Field(default_factory=list)
    samples: int = 0
    model: str = ""


class People(BaseModel):
    schema_version: int = 1
    people: dict[str, Person] = Field(default_factory=dict)


def _path(paths: WorkspacePaths):  # type: ignore[no-untyped-def]
    return paths.people / "voiceprints.json"


def load(paths: WorkspacePaths) -> People:
    path = _path(paths)
    if path.is_file():
        return People.model_validate_json(path.read_text(encoding="utf-8"))
    return People()


def save(paths: WorkspacePaths, people: People) -> None:
    atomic_write_text(_path(paths), people.model_dump_json(indent=1) + "\n")


def record_consent(paths: WorkspacePaths, name: str, note: str) -> Person:
    people = load(paths)
    person = people.people.get(name) or Person(name=name)
    person.consent_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    person.consent_note = note
    people.people[name] = person
    save(paths, people)
    return person


def forget(paths: WorkspacePaths, name: str) -> bool:
    people = load(paths)
    removed = people.people.pop(name, None) is not None
    if removed:
        save(paths, people)
    return removed


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


def enroll(paths: WorkspacePaths, name: str, embedding: list[float], model: str) -> Person:
    """Fold one confirmed speaker embedding into the person's centroid."""
    people = load(paths)
    person = people.people.get(name)
    if person is None or not person.consent_at:
        raise DossierError(
            f"no recorded consent for {name!r}; run `dsr people consent {name!r}` first"
        )
    sample = _unit(embedding)
    if person.centroid and person.model == model and len(person.centroid) == len(sample):
        n = person.samples
        person.centroid = _unit(
            [(c * n + s) / (n + 1) for c, s in zip(person.centroid, sample, strict=True)]
        )
        person.samples = n + 1
    else:
        person.centroid, person.samples, person.model = sample, 1, model
    save(paths, people)
    return person


def voiceprints(paths: WorkspacePaths, model: str) -> dict[str, list[float]]:
    return {
        name: person.centroid
        for name, person in load(paths).people.items()
        if person.consent_at and person.centroid and person.model == model
    }
