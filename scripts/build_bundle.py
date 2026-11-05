#!/usr/bin/env python3
"""Build the shareable bundle: dist/dossier-<version>-bundle.zip.

    python3 scripts/build_bundle.py [--allow-dirty]

The zip holds everything a colleague needs, with no repository access:

    dossier-<version>/
      dossier-<version>-py3-none-any.whl   install with uv (see README)
      dossier-<version>.tar.gz             source distribution
      dossier-<version>.git.bundle         full history: git clone <file> dossier
      Brewfile  README.md  CHANGELOG.md  LICENSE  NOTICE.md  docs/
      SHA256SUMS

It refuses to build from uncommitted changes (the bundle must match a commit)
or when the denylist finds personal or client data in the tree or the history.
Needs git and uv; standard library only otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["Brewfile", "README.md", "CHANGELOG.md", "LICENSE", "NOTICE.md"]


def run(*command: str) -> str:
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        sys.exit(f"{' '.join(command)} failed:\n{completed.stdout}{completed.stderr}")
    return completed.stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--allow-dirty", action="store_true", help="Build from uncommitted changes."
    )
    args = parser.parse_args()

    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if run("git", "status", "--porcelain").strip() and not args.allow_dirty:
        sys.exit("uncommitted changes: commit them first (or pass --allow-dirty)")
    for mode in ("--all", "--history"):
        run(sys.executable, "scripts/denylist.py", mode)

    dist = ROOT / "dist"
    name = f"dossier-{version}"
    stage = dist / name
    shutil.rmtree(stage, ignore_errors=True)
    for old in dist.glob(f"{name}*"):
        old.unlink()
    stage.mkdir(parents=True)

    run("uv", "build", "--out-dir", str(stage))
    run("git", "bundle", "create", str(stage / f"{name}.git.bundle"), "--all")
    for file in FILES:
        shutil.copy2(ROOT / file, stage / file)
    shutil.copytree(ROOT / "docs", stage / "docs")
    (stage / ".gitignore").unlink(missing_ok=True)  # uv build writes one

    artifacts = sorted(
        p for p in stage.iterdir() if p.is_file() and p.suffix in {".whl", ".gz", ".bundle"}
    )
    (stage / "SHA256SUMS").write_text(
        "".join(f"{sha256(p)}  {p.name}\n" for p in artifacts), encoding="utf-8"
    )

    archive = dist / f"{name}-bundle.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                bundle.write(path, f"{name}/{path.relative_to(stage)}")
    commit = run("git", "rev-parse", "--short", "HEAD").strip()
    size = archive.stat().st_size / 1e6
    print(f"{archive.relative_to(ROOT)}  ({size:.1f} MB, commit {commit})")
    print(f"sha256 {sha256(archive)}")


if __name__ == "__main__":
    main()
