"""Windows backends (planned).

The Windows port is not implemented yet. Every capability reports itself as
unavailable with the backend it will use, and process probing is conservative
so a lock held by a live process is never stolen. See ``docs/windows-port.md``.
"""

from __future__ import annotations

from dossier.platform.base import Platform
from dossier.platform.generic import (
    ConservativeProcessProbe,
    LogNotifier,
    NoKeepAwake,
    NoPlaceholders,
    UnavailableOcr,
    UnavailableScheduler,
    UnavailableTts,
)

_PLANNED = "planned for the Windows port"


def build() -> Platform:
    return Platform(
        name="windows",
        process=ConservativeProcessProbe(),
        notifier=LogNotifier(),
        keep_awake=NoKeepAwake(),
        placeholders=NoPlaceholders(),
        ocr=UnavailableOcr(f"Windows.Media.Ocr / Tesseract backend {_PLANNED}"),
        tts=UnavailableTts(f"SAPI backend {_PLANNED}"),
        scheduler=UnavailableScheduler(f"Task Scheduler backend {_PLANNED}"),
    )
