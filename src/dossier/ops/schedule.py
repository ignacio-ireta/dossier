"""``dsr schedule``: run ``dsr sync --scheduled`` daily and/or when sources change."""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from dossier import platform
from dossier.core.io import sha256_text
from dossier.core.policy import require_processing
from dossier.core.workspace import Workspace
from dossier.platform.base import ScheduledJob, ScheduleStatus


def job_label(workspace: Workspace) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", workspace.config.workspace.name.lower()).strip("-")
    digest = sha256_text(str(workspace.root))[:8]
    return f"com.dossier.sync.{slug or 'workspace'}-{digest}"


def dsr_command() -> list[str]:
    found = shutil.which("dsr")
    if found:
        return [str(Path(found).resolve())]
    return [sys.executable, "-m", "dossier"]


def build_job(workspace: Workspace, *, at: tuple[int, int] | None, watch: bool) -> ScheduledJob:
    watch_paths = []
    if watch:
        for source in workspace.config.sources:
            root = workspace.paths.resolve(source.path)
            if root.is_dir():
                watch_paths.append(root)
    return ScheduledJob(
        label=job_label(workspace),
        program=[*dsr_command(), "--workspace", str(workspace.root), "sync", "--scheduled"],
        working_dir=workspace.root,
        log_path=workspace.paths.logs / "schedule.log",
        daily_at=at,
        watch_paths=watch_paths,
        environment={"PATH": os.environ.get("PATH", "")},
    )


def parse_time(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value.strip())
    if not match:
        raise ValueError(f"invalid time {value!r}; use HH:MM (24-hour)")
    return int(match.group(1)), int(match.group(2))


def install(workspace: Workspace, *, at: str | None, watch: bool) -> tuple[ScheduledJob, Path]:
    require_processing(workspace.config, "scheduling unattended sync")
    if at is None and not watch:
        raise ValueError("choose --at HH:MM, --watch, or both")
    job = build_job(workspace, at=parse_time(at) if at else None, watch=watch)
    return job, platform.current().scheduler.install(job)


def remove(workspace: Workspace) -> bool:
    return platform.current().scheduler.remove(job_label(workspace))


def status(workspace: Workspace) -> ScheduleStatus:
    return platform.current().scheduler.status(job_label(workspace))
