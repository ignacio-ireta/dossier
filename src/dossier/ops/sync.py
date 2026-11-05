"""``dsr sync``: run every pipeline stage for a workspace, in order, under one lock.

Stages are independent enough that a failing stage does not stop the next one
(the index can still refresh what conversion did finish). Every real run writes
a content-free report under ``.dossier/runs/``; a dry run writes nothing. On an
unattended (scheduled) run, any problem raises a desktop notification.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from dossier import platform
from dossier.convert.sync import SourceResult, convert_workspace
from dossier.core import logging as dlogging
from dossier.core.errors import EX_FAILURE, EX_OK, EX_TEMPFAIL, DossierError
from dossier.core.lock import WorkspaceLock
from dossier.core.policy import require_processing
from dossier.core.runs import RunReport, StageStatus
from dossier.core.workspace import Workspace

log = logging.getLogger("dossier.sync")

Progress = Callable[[str, str], None]
Stage = Callable[["SyncContext"], dict[str, Any]]


@dataclass
class SyncOptions:
    dry_run: bool = False
    scheduled: bool = False
    confirm_deletes: bool = False
    rehash: bool = False
    retry_failed: bool = False
    sources: list[str] | None = None
    stages: list[str] | None = None  # default: all registered stages


@dataclass
class SyncContext:
    workspace: Workspace
    options: SyncOptions
    report: RunReport
    progress: Progress | None = None
    attention: list[str] = field(default_factory=list)  # needs a human; exit 75


@dataclass
class SyncOutcome:
    report: RunReport
    stages: dict[str, dict[str, Any]]
    attention: list[str]
    exit_status: int


def _convert_stage(ctx: SyncContext) -> dict[str, Any]:
    results: list[SourceResult] = convert_workspace(
        ctx.workspace,
        run_id=ctx.report.run_id,
        sources=ctx.options.sources,
        dry_run=ctx.options.dry_run,
        confirm_deletes=ctx.options.confirm_deletes,
        rehash=ctx.options.rehash,
        retry_failed=ctx.options.retry_failed,
        progress=ctx.progress,
    )
    for result in results:
        if result.blocked:
            ctx.attention.append(f"source {result.source}: {result.blocked}")
    return {
        result.source: {"counts": result.counts, "blocked": result.blocked, "errors": result.errors}
        for result in results
    }


# Ordered registry; later milestones append index and meetings stages.
STAGES: dict[str, Stage] = {"convert": _convert_stage}


def register_stage(name: str, stage: Stage) -> None:
    STAGES[name] = stage


def _counts(data: dict[str, Any]) -> dict[str, int | float]:
    totals: dict[str, int | float] = {}
    for value in data.values():
        if isinstance(value, dict) and isinstance(value.get("counts"), dict):
            for key, number in value["counts"].items():
                if isinstance(number, int | float):
                    totals[key] = totals.get(key, 0) + number
    return totals


def run_sync(
    workspace: Workspace, options: SyncOptions, *, progress: Progress | None = None
) -> SyncOutcome:
    require_processing(workspace.config, "sync")
    report = RunReport("sync")
    ctx = SyncContext(workspace, options, report, progress)
    names = options.stages or list(STAGES)
    unknown = [name for name in names if name not in STAGES]
    if unknown:
        raise DossierError(f"unknown stage(s): {', '.join(unknown)}; known: {', '.join(STAGES)}")
    outputs: dict[str, dict[str, Any]] = {}
    failed = False

    def run_all() -> None:
        nonlocal failed
        for name in names:
            try:
                with report.stage(name) as record:
                    data = STAGES[name](ctx)
                    outputs[name] = data
                    record.counts = _counts(data)
            except DossierError as exc:
                failed = True
                outputs[name] = {"error": exc.as_dict()}
                log.error("stage %s failed: %s", name, exc.message)
            except Exception as exc:
                failed = True
                outputs[name] = {"error": {"code": "unexpected", "message": type(exc).__name__}}
                log.exception("stage %s failed unexpectedly", name)

    if options.dry_run:
        run_all()
    else:
        dlogging.configure(log_dir=workspace.paths.logs)
        with (
            WorkspaceLock(workspace.paths.locks, "sync", run_id=report.run_id),
            platform.current().keep_awake.hold("dsr sync"),
        ):
            run_all()
    if any(record.status is StageStatus.FAILED for record in report.stages):
        failed = True
    exit_status = EX_FAILURE if failed else (EX_TEMPFAIL if ctx.attention else EX_OK)
    if not options.dry_run:
        report.write(workspace.paths.runs, exit_status=exit_status)
        if options.scheduled and exit_status != EX_OK:
            platform.current().notifier.notify(
                f"dossier: {workspace.config.workspace.name}",
                "sync failed" if failed else "sync needs attention: " + ctx.attention[0][:200],
            )
    return SyncOutcome(report, outputs, ctx.attention, exit_status)
