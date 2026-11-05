"""Single-writer lockfiles under ``<workspace>/.dossier/locks/``.

A lock is published atomically with its full holder payload (hard link of a
fully written temp file, or O_CREAT|O_EXCL where hard links are unavailable),
so a concurrent reader never sees a half-written lockfile. A dead pid on the
same host is stale and replaced; liveness cannot be probed across hosts, so a
foreign hostname counts as live. Liveness checks go through the platform seam,
which answers "alive" whenever it cannot tell.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from pydantic import BaseModel

from dossier import platform
from dossier.core.errors import LockedError

log = logging.getLogger("dossier.lock")


class LockHolder(BaseModel):
    schema_version: int = 1
    pid: int
    hostname: str
    run_id: str = ""
    started_at: datetime | None = None


def _holder_is_stale(path: Path) -> bool:
    try:
        holder = LockHolder.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True  # unreadable or malformed: no live holder can prove itself
    if holder.hostname != socket.gethostname():
        return False
    return not platform.current().process.pid_alive(holder.pid)


class WorkspaceLock:
    """Context manager owning one named lockfile."""

    def __init__(self, locks_dir: Path, name: str, *, run_id: str = "", wait_s: float = 0.0):
        self.path = locks_dir / f"{name}.lock"
        self.name = name
        self.run_id = run_id
        self.wait_s = wait_s
        self._held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            LockHolder(
                pid=os.getpid(),
                hostname=socket.gethostname(),
                run_id=self.run_id,
                started_at=datetime.now(UTC),
            ).model_dump_json(indent=2)
            + "\n"
        )
        deadline = time.monotonic() + self.wait_s
        while True:
            if self._try_publish(payload):
                self._held = True
                return
            if _holder_is_stale(self.path):
                self._claim_stale()
                continue
            if time.monotonic() < deadline:
                time.sleep(0.05)
                continue
            raise LockedError(
                f"{self.name!r} is locked by another dsr run ({self.path}); wait for it to "
                "finish, or delete the lock file if that run is gone",
                path=self.path,
            )

    def _try_publish(self, payload: str) -> bool:
        fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            try:
                os.link(tmp, self.path)
                return True
            except FileExistsError:
                return False
            except OSError:
                try:
                    raw = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    return False
                with os.fdopen(raw, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                return True
        finally:
            tmp.unlink(missing_ok=True)

    def _claim_stale(self) -> None:
        """Remove a stale lock without ever unlinking a live one (rename, re-verify)."""
        claim = self.path.with_name(f".{self.path.name}.claim.{os.getpid()}")
        try:
            self.path.rename(claim)
        except FileNotFoundError:
            return
        except FileExistsError:  # Windows: rename never overwrites
            claim.unlink(missing_ok=True)
            return
        if _holder_is_stale(claim):
            log.warning("removed stale lock %s", self.path)
        else:
            with contextlib.suppress(FileExistsError, OSError):
                os.link(claim, self.path)
        claim.unlink(missing_ok=True)

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            holder = LockHolder.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.path.unlink(missing_ok=True)
            return
        if holder.pid == os.getpid() and holder.hostname == socket.gethostname():
            self.path.unlink(missing_ok=True)
        else:
            log.warning("lock %s no longer ours (pid %d); leaving it", self.path, holder.pid)

    def __enter__(self) -> WorkspaceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
