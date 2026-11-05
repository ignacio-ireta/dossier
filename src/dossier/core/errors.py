"""Error hierarchy. Every error carries a stable code and a process exit status.

Exit statuses follow BSD sysexits where one fits, so schedulers and scripts can
tell "blocked by policy" (78) from "try again later" (75) from a real failure.
"""

from __future__ import annotations

from pathlib import Path

EX_OK = 0
EX_FAILURE = 1
EX_USAGE = 2
EX_NOTHING_TO_DO = 4
EX_UNAVAILABLE = 69
EX_TEMPFAIL = 75
EX_CONFIG = 78


class DossierError(Exception):
    """Base error. ``code`` is a short machine-readable identifier."""

    code = "error"
    exit_status = EX_FAILURE

    def __init__(self, message: str, *, path: Path | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.path = path

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "code": self.code,
            "message": self.message,
            "path": str(self.path) if self.path else None,
            "exit_status": self.exit_status,
        }


class ConfigError(DossierError):
    code = "config_invalid"
    exit_status = EX_CONFIG


class WorkspaceNotFoundError(DossierError):
    code = "workspace_not_found"
    exit_status = EX_USAGE


class PolicyBlockedError(DossierError):
    """The workspace policy does not permit this action. Nothing was processed."""

    code = "policy_blocked"
    exit_status = EX_CONFIG


class LockedError(DossierError):
    code = "locked"
    exit_status = EX_TEMPFAIL


class ProviderUnavailableError(DossierError):
    """A model provider could not be reached (server down, timeout, bad URL)."""

    code = "provider_unavailable"
    exit_status = EX_UNAVAILABLE


class ProviderResponseError(DossierError):
    """A provider answered, but with an error or an unusable payload."""

    code = "provider_error"
    exit_status = EX_FAILURE

    def __init__(
        self, message: str, *, status: int | None = None, path: Path | None = None
    ) -> None:
        super().__init__(message, path=path)
        self.status = status


class CapabilityUnavailableError(DossierError):
    """A platform capability (OCR, scheduler, TTS...) has no backend on this OS."""

    code = "capability_unavailable"
    exit_status = EX_UNAVAILABLE


class ExternalToolError(DossierError):
    """A required external program is missing or failed."""

    code = "external_tool"
    exit_status = EX_UNAVAILABLE
