"""Template packs: loading, inheritance, resolution, inspection and import.

``pack.toml``::

    name = "acme"
    extends = "neutral"            # optional; missing kinds and fields come from here
    default_kind = "report"

    [defaults]                     # values for {{tokens}} unless the document sets them
    author = "Acme Consulting"
    doc_code = "ACME-DOC"

    [kinds.report]
    docx = "report/reference.docx" # styles, header and footer for DOCX (and PDF via DOCX)
    cover = "report/cover.docx"    # optional first page with {{title}} ... tokens
    toc = true
    [kinds.deck]
    pptx = "deck/template.pptx"
    [kinds.report.pdf_vars]
    papersize = "a4"

Resolution for a render: ``--template pack:kind``, else the document's
``template:`` front matter, else the workspace ``[docs] template``, else
``neutral``; the kind comes from the same places, then the pack's default.
"""

from __future__ import annotations

import re
import shutil
import tomllib
import zipfile
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Literal

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dossier.core.errors import ConfigError, DossierError
from dossier.core.io import atomic_write_text
from dossier.core.paths import user_templates_dir
from dossier.core.workspace import Workspace

BUILTIN = "neutral"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
Origin = Literal["workspace", "user", "builtin"]
PANDOC_PPTX_LAYOUTS = (
    "Title Slide",
    "Title and Content",
    "Section Header",
    "Two Content",
    "Comparison",
    "Content with Caption",
    "Blank",
)
REQUIRED_DOCX_STYLES = ("Title", "Heading 1", "Heading 2", "Body Text", "Table")
TOKEN = re.compile(r"\{\{\s*([a-zA-Z_][\w-]*)\s*\}\}")


class KindSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    docx: str | None = None
    cover: str | None = None
    pptx: str | None = None
    pdf_theme: str | None = None
    pdf_vars: dict[str, str] = Field(default_factory=dict)
    toc: bool | None = None
    number_sections: bool | None = None
    description: str = ""


class PackSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    version: str = "1"
    description: str = ""
    extends: str | None = None
    default_kind: str = "report"
    defaults: dict[str, str] = Field(default_factory=dict)
    kinds: dict[str, KindSpec] = Field(default_factory=dict)


@dataclass
class Pack:
    name: str
    root: Path
    spec: PackSpec
    origin: Origin
    parent: Pack | None = None

    def chain(self) -> list[Pack]:
        packs: list[Pack] = []
        current: Pack | None = self
        while current is not None:
            packs.append(current)
            current = current.parent
        return packs

    def kind_names(self) -> list[str]:
        names: list[str] = []
        for pack in self.chain():
            names += [k for k in pack.spec.kinds if k not in names]
        return names

    def defaults(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for pack in reversed(self.chain()):
            merged.update(pack.spec.defaults)
        return merged

    def resolve_kind(self, kind: str) -> ResolvedKind:
        if kind not in self.kind_names():
            raise DossierError(
                f"template pack {self.name!r} has no kind {kind!r}; "
                f"available: {', '.join(self.kind_names())}"
            )
        resolved = ResolvedKind(pack=self.name, kind=kind, defaults=self.defaults())
        for pack in reversed(self.chain()):  # parents first, children override
            spec = pack.spec.kinds.get(kind)
            if spec is None:
                continue
            for attr in ("docx", "cover", "pptx", "pdf_theme"):
                value = getattr(spec, attr)
                if value:
                    setattr(resolved, attr, pack.root / value)
            if spec.toc is not None:
                resolved.toc = spec.toc
            if spec.number_sections is not None:
                resolved.number_sections = spec.number_sections
            resolved.pdf_vars.update(spec.pdf_vars)
        return resolved


@dataclass
class ResolvedKind:
    pack: str
    kind: str
    docx: Path | None = None
    cover: Path | None = None
    pptx: Path | None = None
    pdf_theme: Path | None = None
    pdf_vars: dict[str, str] = field(default_factory=dict)
    toc: bool = False
    number_sections: bool = False
    defaults: dict[str, str] = field(default_factory=dict)


def builtin_dir() -> Path:
    return Path(str(resources.files("dossier.docs") / "packs"))


def search_dirs(workspace: Workspace | None) -> list[tuple[Origin, Path]]:
    dirs: list[tuple[Origin, Path]] = []
    if workspace is not None:
        dirs.append(("workspace", workspace.paths.templates))
    dirs += [("user", user_templates_dir()), ("builtin", builtin_dir())]
    return dirs


def _read_spec(path: Path) -> PackSpec:
    try:
        return PackSpec.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
    except (tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ConfigError(f"invalid template pack {path}: {exc}", path=path) from exc


def load_pack(name: str, workspace: Workspace | None, _seen: tuple[str, ...] = ()) -> Pack:
    if name in _seen:
        raise ConfigError(f"template packs extend each other in a loop: {' -> '.join(_seen)}")
    for origin, directory in search_dirs(workspace):
        manifest = directory / name / "pack.toml"
        if manifest.is_file():
            spec = _read_spec(manifest)
            parent = load_pack(spec.extends, workspace, (*_seen, name)) if spec.extends else None
            return Pack(name=name, root=manifest.parent, spec=spec, origin=origin, parent=parent)
    raise DossierError(f"template pack {name!r} not found; see `dsr templates list`")


def list_packs(workspace: Workspace | None) -> list[Pack]:
    seen: dict[str, Pack] = {}
    for _origin, directory in search_dirs(workspace):
        if not directory.is_dir():
            continue
        for manifest in sorted(directory.glob("*/pack.toml")):
            name = manifest.parent.name
            if name not in seen:
                seen[name] = load_pack(name, workspace)
    return list(seen.values())


def parse_reference(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    pack, _, kind = value.partition(":")
    return pack.strip() or None, kind.strip() or None


def resolve(
    workspace: Workspace | None,
    *,
    template: str | None,
    front: dict[str, str],
    output: str,
) -> ResolvedKind:
    cli_pack, cli_kind = parse_reference(template)
    fm_pack, fm_kind = parse_reference(front.get("template"))
    configured = workspace.config.docs.template if workspace else None
    cfg_pack, cfg_kind = parse_reference(configured)
    pack = load_pack(cli_pack or fm_pack or cfg_pack or BUILTIN, workspace)
    kind = cli_kind or fm_kind or front.get("kind") or cfg_kind or pack.spec.default_kind
    resolved = pack.resolve_kind(kind)
    if output == "pptx" and resolved.pptx is None and not (cli_kind or fm_kind):
        for candidate in ("deck", *pack.kind_names()):
            if candidate in pack.kind_names() and pack.resolve_kind(candidate).pptx:
                return pack.resolve_kind(candidate)
    return resolved


@dataclass(frozen=True)
class Finding:
    level: Literal["ok", "warn", "error"]
    kind: str
    message: str


def docx_tokens(path: Path) -> tuple[set[str], int]:
    """Tokens in a DOCX's body, headers and footers, and how many look split across runs."""
    tokens: set[str] = set()
    split = 0
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not re.fullmatch(r"word/(document|header\d*|footer\d*)\.xml", name):
                continue
            xml = archive.read(name).decode("utf-8", errors="replace")
            tokens |= set(TOKEN.findall(xml))
            text = re.sub(r"<[^>]+>", "", xml)
            split += max(0, len(TOKEN.findall(text)) - len(TOKEN.findall(xml)))
    return tokens, split


def inspect_pack(pack: Pack) -> list[Finding]:
    import docx
    from pptx import Presentation

    findings: list[Finding] = []
    for kind in pack.kind_names():
        resolved = pack.resolve_kind(kind)
        if not any((resolved.docx, resolved.pptx, resolved.pdf_theme)):
            findings.append(Finding("error", kind, "names no docx, pptx or pdf_theme file"))
        for attr in ("docx", "cover", "pptx", "pdf_theme"):
            path = getattr(resolved, attr)
            if path is not None and not path.is_file():
                findings.append(Finding("error", kind, f"{attr} file missing: {path}"))
        if resolved.docx and resolved.docx.is_file():
            try:
                styles = {s.name for s in docx.Document(str(resolved.docx)).styles}
            except Exception as exc:
                findings.append(Finding("error", kind, f"docx unreadable: {type(exc).__name__}"))
            else:
                missing = [s for s in REQUIRED_DOCX_STYLES if s not in styles]
                findings.append(
                    Finding("warn", kind, f"docx lacks styles: {', '.join(missing)}")
                    if missing
                    else Finding("ok", kind, "docx styles complete")
                )
            tokens, split = docx_tokens(resolved.docx)
            if tokens:
                findings.append(Finding("ok", kind, f"docx tokens: {', '.join(sorted(tokens))}"))
            if split:
                findings.append(
                    Finding(
                        "warn",
                        kind,
                        f"{split} token(s) split across runs; re-import with --replace to repair",
                    )
                )
        if resolved.cover and resolved.cover.is_file():
            tokens, split = docx_tokens(resolved.cover)
            findings.append(
                Finding(
                    "ok" if tokens and not split else "warn",
                    kind,
                    f"cover tokens: {', '.join(sorted(tokens)) or 'none'}"
                    + (f"; {split} split" if split else ""),
                )
            )
        if resolved.pptx and resolved.pptx.is_file():
            try:
                names = {layout.name for layout in Presentation(str(resolved.pptx)).slide_layouts}
            except Exception as exc:
                findings.append(Finding("error", kind, f"pptx unreadable: {type(exc).__name__}"))
            else:
                missing = [n for n in PANDOC_PPTX_LAYOUTS if n not in names]
                findings.append(
                    Finding(
                        "warn", kind, "pptx lacks layouts (defaults used): " + ", ".join(missing)
                    )
                    if missing
                    else Finding("ok", kind, "pptx layouts complete")
                )
        if resolved.pdf_theme and resolved.pdf_theme.is_file():
            text = resolved.pdf_theme.read_text(encoding="utf-8", errors="replace")
            findings.append(
                Finding("ok", kind, "pdf theme defines conf")
                if "#let conf(" in text
                else Finding("error", kind, "pdf theme must define `#let conf(...)`")
            )
    return findings


def new_pack(workspace: Workspace, name: str, *, extends: str | None = BUILTIN) -> Path:
    if not _NAME.match(name):
        raise DossierError("pack names use lowercase letters, digits, - and _")
    root = workspace.paths.templates / name
    if (root / "pack.toml").exists():
        raise DossierError(f"template pack {name!r} already exists at {root}")
    if extends:
        load_pack(extends, workspace)  # must exist
    document = tomlkit.document()
    document["name"] = name
    document["version"] = "1"
    if extends:
        document["extends"] = extends
    document["default_kind"] = "report"
    document["defaults"] = tomlkit.table()
    document["kinds"] = tomlkit.table()
    root.mkdir(parents=True, exist_ok=True)
    atomic_write_text(root / "pack.toml", tomlkit.dumps(document))
    return root


ROLES = {".docx": "docx", ".dotx": "docx", ".pptx": "pptx", ".potx": "pptx", ".typ": "pdf_theme"}


def import_file(
    workspace: Workspace,
    source: Path,
    *,
    pack: str,
    kind: str,
    role: str | None = None,
    replacements: dict[str, str] | None = None,
) -> Path:
    """Copy a branded file into a workspace pack (creating the pack if needed)."""
    from dossier.docs.docx_tools import tokenize

    if not source.is_file():
        raise DossierError(f"{source} not found")
    role = role or ROLES.get(source.suffix.lower())
    if role not in {"docx", "cover", "pptx", "pdf_theme"}:
        raise DossierError("role must be docx, cover, pptx or pdf_theme (or use a known suffix)")
    root = workspace.paths.templates / pack
    if not (root / "pack.toml").is_file():
        new_pack(workspace, pack)
    suffix = {"docx": ".docx", "cover": ".docx", "pptx": ".pptx", "pdf_theme": ".typ"}[role]
    file_name = {"docx": "reference", "cover": "cover", "pptx": "template", "pdf_theme": "theme"}[
        role
    ] + suffix
    target = root / kind / file_name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if replacements and suffix == ".docx":
        tokenize(target, replacements)
    document = tomlkit.parse((root / "pack.toml").read_text(encoding="utf-8"))
    kinds = document.setdefault("kinds", tomlkit.table())
    entry = kinds.setdefault(kind, tomlkit.table())
    entry[role] = f"{kind}/{file_name}"
    atomic_write_text(root / "pack.toml", tomlkit.dumps(document))
    return target
