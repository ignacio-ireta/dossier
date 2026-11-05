"""Incremental conversion of every source folder into ``corpus/``.

Per source: discover files, hash what changed (size + mtime short-circuit),
diff against the snapshot, then

- convert added and updated files,
- relocate moved files (same content, new path) without re-extraction,
- archive the outputs of deleted files under ``.dossier/archive/<run>/``.

Safety: a missing source folder, an emptied one, or a run that would delete more
than ``convert.mass_delete_ratio`` of a source is blocked unless the caller
confirms deletions. A dry run computes the plan and writes nothing.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from dossier.convert import markdown
from dossier.convert.discovery import Candidate, Discovery, discover
from dossier.convert.extractors import (
    ExtractContext,
    ExtractionError,
    extractor_for,
    extractor_id,
)
from dossier.convert.normalize import normalize
from dossier.convert.snapshot import FileState, Snapshot, load_snapshot, save_snapshot
from dossier.core.config import SourceRoot
from dossier.core.io import atomic_write_json, atomic_write_text, sha256_file, sha256_text
from dossier.core.workspace import Workspace

log = logging.getLogger("dossier.convert")

MIN_FILES_FOR_RATIO_GUARD = 10
_SAVE_EVERY = 25
_UNSAFE = re.compile(r"[^\w.\- ]+", re.UNICODE)

Progress = Callable[[str, str], None]  # (event, relpath)


@dataclass
class Plan:
    source: str
    root: Path
    discovery: Discovery
    hashes: dict[str, str] = field(default_factory=dict)
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    moved: list[tuple[str, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    blocked: str | None = None


@dataclass
class SourceResult:
    source: str
    counts: dict[str, int] = field(default_factory=dict)
    blocked: str | None = None
    errors: list[dict[str, str]] = field(default_factory=list)


def corpus_paths(source: str, relpath: str) -> tuple[str, str, str]:
    """Corpus-relative paths of the Markdown, the meta sidecar and the sheets folder."""
    base = f"{source}/{relpath}"
    return f"{base}.md", f"{base}.meta.json", f"{base}.sheets"


def plan_source(
    workspace: Workspace,
    source: SourceRoot,
    *,
    confirm_deletes: bool = False,
    rehash: bool = False,
    retry_failed: bool = False,
) -> tuple[Plan, Snapshot]:
    root = workspace.paths.resolve(source.path)
    convert = workspace.config.convert
    if not root.is_dir():
        snapshot = load_snapshot(workspace.paths, source.name, root)
        plan = Plan(source.name, root, Discovery(root=root))
        plan.blocked = f"source folder {root} is missing (unmounted or offline?)"
        return plan, snapshot
    found = discover(root, source, max_bytes=convert.max_file_mb * 1024 * 1024)
    snapshot = load_snapshot(workspace.paths, source.name, root)
    plan = Plan(source.name, root, found)
    seen: dict[str, Candidate] = {c.relpath: c for c in found.documents}
    for relpath, candidate in seen.items():
        previous = snapshot.files.get(relpath)
        reusable = (
            previous is not None
            and not rehash
            and previous.size == candidate.size
            and previous.mtime_ns == candidate.mtime_ns
        )
        plan.hashes[relpath] = (
            previous.sha256 if reusable and previous else sha256_file(candidate.path)
        )
    new = [relpath for relpath in seen if relpath not in snapshot.files]
    gone = [relpath for relpath in snapshot.files if relpath not in seen]
    for relpath in seen:
        previous = snapshot.files.get(relpath)
        if previous is None:
            continue
        extractor = extractor_for(seen[relpath].path)
        current_id = extractor_id(extractor) if extractor else ""
        changed = previous.sha256 != plan.hashes[relpath] or previous.extractor != current_id
        if changed or (retry_failed and previous.status == "failed"):
            plan.updated.append(relpath)
        else:
            plan.unchanged.append(relpath)
    gone_by_hash: dict[str, list[str]] = {}
    for relpath in gone:
        state = snapshot.files[relpath]
        if state.status == "ok":
            gone_by_hash.setdefault(state.sha256, []).append(relpath)
    for relpath in new:
        candidates = gone_by_hash.get(plan.hashes[relpath])
        if candidates:
            old = candidates.pop(0)
            plan.moved.append((old, relpath))
        else:
            plan.added.append(relpath)
    moved_from = {old for old, _ in plan.moved}
    plan.deleted = [relpath for relpath in gone if relpath not in moved_from]

    previous_count = len(snapshot.files)
    if plan.deleted and not confirm_deletes:
        if not found.documents and previous_count:
            plan.blocked = (
                f"source folder is empty but {previous_count} files were converted before; "
                "pass --confirm-deletes if they were really removed"
            )
        elif (
            previous_count >= MIN_FILES_FOR_RATIO_GUARD
            and len(plan.deleted) / previous_count > convert.mass_delete_ratio
        ):
            plan.blocked = (
                f"{len(plan.deleted)} of {previous_count} files would be deleted; "
                "pass --confirm-deletes if that is intended"
            )
    return plan, snapshot


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(rows)
    atomic_write_text(path, buffer.getvalue())


def _sheet_file_name(name: str, used: set[str]) -> str:
    base = _UNSAFE.sub("_", name).strip(" ._") or "sheet"
    candidate, number = f"{base}.csv", 2
    while candidate.lower() in used:
        candidate, number = f"{base}-{number}.csv", number + 1
    used.add(candidate.lower())
    return candidate


def convert_file(
    workspace: Workspace,
    source: str,
    candidate: Candidate,
    sha256: str,
    ctx: ExtractContext,
    run_id: str,
) -> FileState:
    extractor = extractor_for(candidate.path)
    assert extractor is not None
    result = extractor.extract(candidate.path, ctx)
    document = normalize(result.document)
    corpus = workspace.paths.corpus
    md_rel, meta_rel, sheets_rel = corpus_paths(source, candidate.relpath)
    sheets_dir = corpus / sheets_rel
    if sheets_dir.exists():
        shutil.rmtree(sheets_dir)
    sheet_csvs: dict[str, str] = {}
    sheet_meta: list[dict[str, object]] = []
    extra: list[str] = [meta_rel]
    used: set[str] = set()
    for sheet in result.sheets:
        file_name = _sheet_file_name(sheet.name, used)
        relative = f"{sheets_rel}/{file_name}"
        _write_csv(corpus / relative, sheet.rows)
        sheet_csvs[sheet.name] = f"corpus/{relative}"
        extra.append(relative)
        sheet_meta.append(
            {
                "name": sheet.name,
                "csv": relative,
                "rows": max(0, len(sheet.rows) - 1),
                "columns": max((len(row) for row in sheet.rows), default=0),
            }
        )
    file_name = PurePosixPath(candidate.relpath).name
    front = {
        "source": f"{source}/{candidate.relpath}",
        "sha256": sha256,
        "type": document.source_type,
        "extractor": extractor_id(extractor),
        "title": document.title,
        "sections": len(document.sections),
        "warnings": len(result.warnings),
    }
    text = markdown.render(
        document,
        file_name=file_name,
        meta=front,
        warnings=result.warnings,
        sheet_csvs=sheet_csvs,
    )
    atomic_write_text(corpus / md_rel, text)
    atomic_write_json(
        corpus / meta_rel,
        {
            "source": source,
            "relpath": candidate.relpath,
            "sha256": sha256,
            "size": candidate.size,
            "type": document.source_type,
            "extractor": extractor_id(extractor),
            "title": document.title,
            "sections": [
                {"kind": s.kind.value, "index": s.index, "title": s.title}
                for s in document.sections
            ],
            "warnings": [w.model_dump() for w in result.warnings],
            "sheets": sheet_meta,
            "words": len(text.split()),
            "run_id": run_id,
        },
    )
    return FileState(
        sha256=sha256,
        size=candidate.size,
        mtime_ns=candidate.mtime_ns,
        extractor=extractor_id(extractor),
        status="ok",
        output=md_rel,
        output_sha256=sha256_text(text),
        extra_outputs=extra,
        title=document.title,
        run_id=run_id,
    )


def _outputs(state: FileState) -> list[str]:
    outputs = [state.output] if state.output else []
    return outputs + list(state.extra_outputs)


def _archive(workspace: Workspace, state: FileState, run_id: str) -> None:
    corpus = workspace.paths.corpus
    archive = workspace.paths.state / "archive" / run_id
    sheet_dirs: set[str] = set()
    for relative in _outputs(state):
        source = corpus / relative
        if relative.endswith(".csv"):
            sheet_dirs.add(str(PurePosixPath(relative).parent))
        if source.exists():
            target = archive / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
    for directory in sheet_dirs:
        folder = corpus / directory
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()


def _relocate(
    workspace: Workspace, source: str, state: FileState, old: str, new: str, run_id: str
) -> FileState:
    corpus = workspace.paths.corpus
    old_md, old_meta, old_sheets = corpus_paths(source, old)
    new_md, new_meta, new_sheets = corpus_paths(source, new)
    moves = [(old_md, new_md), (old_meta, new_meta), (old_sheets, new_sheets)]
    for before, after in moves:
        origin = corpus / before
        if origin.exists():
            target = corpus / after
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(origin), str(target))
    md_path = corpus / new_md
    output_sha = state.output_sha256
    if md_path.is_file():
        text = md_path.read_text(encoding="utf-8")
        text = text.replace(f'source: "{source}/{old}"', f'source: "{source}/{new}"', 1).replace(
            f"corpus/{old_sheets}/", f"corpus/{new_sheets}/"
        )
        atomic_write_text(md_path, text)
        output_sha = sha256_text(text)
    meta_path = corpus / new_meta
    if meta_path.is_file():
        import json

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["relpath"] = new
        for sheet in meta.get("sheets", []):
            sheet["csv"] = str(sheet["csv"]).replace(old_sheets, new_sheets, 1)
        atomic_write_json(meta_path, meta)
    extra = [
        item.replace(old_sheets, new_sheets, 1) if item.startswith(old_sheets) else new_meta
        for item in state.extra_outputs
    ]
    return state.model_copy(
        update={
            "output": new_md,
            "output_sha256": output_sha,
            "extra_outputs": extra,
            "run_id": run_id,
        }
    )


def execute_plan(
    workspace: Workspace,
    plan: Plan,
    snapshot: Snapshot,
    *,
    run_id: str,
    progress: Progress | None = None,
) -> SourceResult:
    result = SourceResult(source=plan.source)
    config = workspace.config
    ctx = ExtractContext(
        ocr=config.convert.ocr,
        languages=config.workspace.languages,
        max_sheet_rows=config.convert.max_sheet_rows,
        preview_rows=config.convert.preview_rows,
    )
    counts = {"added": 0, "updated": 0, "moved": 0, "deleted": 0, "failed": 0}
    for old, new in plan.moved:
        snapshot.files[new] = _relocate(
            workspace, plan.source, snapshot.files.pop(old), old, new, run_id
        )
        counts["moved"] += 1
        if progress:
            progress("moved", new)
    for relpath in plan.deleted:
        _archive(workspace, snapshot.files.pop(relpath), run_id)
        counts["deleted"] += 1
        if progress:
            progress("deleted", relpath)
    candidates = {c.relpath: c for c in plan.discovery.documents}
    work = [(r, "added") for r in plan.added] + [(r, "updated") for r in plan.updated]
    for done, (relpath, kind) in enumerate(work, start=1):
        candidate = candidates[relpath]
        try:
            state = convert_file(
                workspace, plan.source, candidate, plan.hashes[relpath], ctx, run_id
            )
        except ExtractionError as exc:
            state = _failed(candidate, plan.hashes[relpath], exc.code, run_id)
            result.errors.append({"path": relpath, "code": exc.code, "message": exc.message})
        except Exception as exc:  # one bad file must not stop the source
            log.warning("unexpected error converting %s: %s", relpath, type(exc).__name__)
            code = f"unexpected:{type(exc).__name__}"
            state = _failed(candidate, plan.hashes[relpath], code, run_id)
            result.errors.append({"path": relpath, "code": code, "message": type(exc).__name__})
        previous = snapshot.files.get(relpath)
        if state.status == "failed" and previous is not None and previous.status == "ok":
            _archive(workspace, previous, run_id)  # stale output no longer matches the source
        snapshot.files[relpath] = state
        counts["failed" if state.status == "failed" else kind] += 1
        if progress:
            progress("failed" if state.status == "failed" else kind, relpath)
        if done % _SAVE_EVERY == 0:
            save_snapshot(workspace.paths, snapshot, run_id)
    save_snapshot(workspace.paths, snapshot, run_id)
    errors_path = workspace.paths.state / "state" / "convert" / f"{plan.source}.errors.json"
    atomic_write_json(
        errors_path,
        [
            {"path": relpath, "code": state.error_code}
            for relpath, state in sorted(snapshot.files.items())
            if state.status == "failed"
        ],
    )
    counts["unchanged"] = len(plan.unchanged)
    counts["media"] = len(plan.discovery.media)
    counts.update({f"skipped_{k}": v for k, v in plan.discovery.skipped.items()})
    result.counts = counts
    return result


def _failed(candidate: Candidate, sha256: str, code: str, run_id: str) -> FileState:
    extractor = extractor_for(candidate.path)
    return FileState(
        sha256=sha256,
        size=candidate.size,
        mtime_ns=candidate.mtime_ns,
        extractor=extractor_id(extractor) if extractor else "",
        status="failed",
        error_code=code,
        run_id=run_id,
    )


def plan_counts(plan: Plan) -> dict[str, int]:
    counts = {
        "added": len(plan.added),
        "updated": len(plan.updated),
        "moved": len(plan.moved),
        "deleted": len(plan.deleted),
        "unchanged": len(plan.unchanged),
        "media": len(plan.discovery.media),
    }
    counts.update({f"skipped_{k}": v for k, v in plan.discovery.skipped.items()})
    return counts


def convert_workspace(
    workspace: Workspace,
    *,
    run_id: str,
    sources: list[str] | None = None,
    dry_run: bool = False,
    confirm_deletes: bool = False,
    rehash: bool = False,
    retry_failed: bool = False,
    progress: Progress | None = None,
) -> list[SourceResult]:
    selected = [s for s in workspace.config.sources if sources is None or s.name in sources]
    unknown = set(sources or []) - {s.name for s in workspace.config.sources}
    if unknown:
        from dossier.core.errors import ConfigError

        raise ConfigError(f"unknown source(s): {', '.join(sorted(unknown))}")
    results: list[SourceResult] = []
    for source in selected:
        plan, snapshot = plan_source(
            workspace,
            source,
            confirm_deletes=confirm_deletes,
            rehash=rehash,
            retry_failed=retry_failed,
        )
        if plan.blocked or dry_run:
            results.append(
                SourceResult(source=source.name, counts=plan_counts(plan), blocked=plan.blocked)
            )
            continue
        results.append(execute_plan(workspace, plan, snapshot, run_id=run_id, progress=progress))
    return results
