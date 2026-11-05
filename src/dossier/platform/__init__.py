"""Platform seam: pick the OS backend once, let tests swap it.

Usage::

    from dossier import platform
    platform.current().notifier.notify("dsr sync", "finished")
"""

from __future__ import annotations

import sys

from dossier.platform.base import Platform

_override: Platform | None = None
_cached: Platform | None = None


def _detect() -> Platform:
    if sys.platform == "darwin":
        from dossier.platform import macos

        return macos.build()
    if sys.platform == "win32":
        from dossier.platform import windows

        return windows.build()
    from dossier.platform import generic

    return generic.build()


def current() -> Platform:
    global _cached
    if _override is not None:
        return _override
    if _cached is None:
        _cached = _detect()
    return _cached


def use(platform: Platform | None) -> None:
    """Install a platform (tests) or restore detection with ``None``."""
    global _override, _cached
    _override = platform
    _cached = None


__all__ = ["Platform", "current", "use"]
