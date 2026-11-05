#!/usr/bin/env python3
"""Block personal data and project-specific names from entering the repository.

Two layers of rules:

* Built-in, generic: email addresses (except reserved example domains) and
  home-directory paths that name a real account.
* Personal, kept *outside* the repository so the repo never contains the names
  it blocks: one term per line in ``$DOSSIER_DENYLIST`` or
  ``~/.config/dossier-dev/denylist.txt``. Terms match case- and
  accent-insensitively on word boundaries; a line starting with ``re:`` is a
  regular expression; ``#`` starts a comment.

A line containing ``denylist-ok`` is skipped (use sparingly, e.g. for a lint code
that looks like an identifier).

Usage::

    scripts/denylist.py --staged            # pre-commit: staged content + author identity
    scripts/denylist.py --message FILE      # commit-msg hook
    scripts/denylist.py --all               # every tracked and untracked (not ignored) file
    scripts/denylist.py --history           # every commit: metadata, messages, diffs

Standard library only, Python 3.9+, so hooks run anywhere.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
import unicodedata
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from re import Pattern

ALLOWED_EMAIL_DOMAINS = (
    "example.com",
    "example.org",
    "example.net",
    "anthropic.com",
    "users.noreply.github.com",
    "localhost",
)
ALLOWED_EMAIL_SUFFIXES = (".invalid", ".example", ".test", ".localhost")
PLACEHOLDER_USERS = {
    "you",
    "me",
    "user",
    "username",
    "name",
    "runner",
    "example",
    "shared",
    "public",
    "someone",
    "alice",
    "bob",
    "dev",
    "default",
    "admin",
    "home",
    "tmp",
}

# The local part must contain a letter or digit, so a diff line such as
# "+@pytest.fixture" is not mistaken for an address.
EMAIL = re.compile(
    r"[A-Za-z0-9._%+-]*[A-Za-z0-9][A-Za-z0-9._%+-]*@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)"
)
HOME_PATH = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\+Users\\+)([A-Za-z0-9._-]+)")
SKIP_MARK = "denylist-ok"
MAX_BYTES = 20 * 1024 * 1024

Finding = tuple[str, int, str]


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def denylist_path() -> Path:
    env = os.environ.get("DOSSIER_DENYLIST")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "dossier-dev" / "denylist.txt"


def load_terms(path: Path) -> list[tuple[str, Pattern[str]]]:
    terms: list[tuple[str, Pattern[str]]] = []
    if not path.is_file():
        return terms
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            terms.append(("pattern", re.compile(line[3:])))
        else:
            folded = re.escape(fold(line))
            terms.append(("term", re.compile(rf"(?<![0-9a-z]){folded}(?![0-9a-z])")))
    return terms


def email_allowed(domain: str) -> bool:
    domain = domain.lower()
    if re.fullmatch(r"[0-9.]+", domain):
        return True  # user:pass@127.0.0.1 is URL credentials, not an address
    if domain.endswith(ALLOWED_EMAIL_SUFFIXES):
        return True
    return any(domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS)


def scan_text(label: str, text: str, terms: list[tuple[str, Pattern[str]]]) -> Iterator[Finding]:
    for number, line in enumerate(text.splitlines(), start=1):
        if SKIP_MARK in line:
            continue
        for match in EMAIL.finditer(line):
            if not email_allowed(match.group(1)):
                yield (label, number, f"email address {match.group(0)!r}")
        for match in HOME_PATH.finditer(line):
            if match.group(1).lower() not in PLACEHOLDER_USERS:
                yield (label, number, f"home path naming an account {match.group(0)!r}")
        folded = fold(line)
        for kind, pattern in terms:
            haystack = line if kind == "pattern" else folded
            hit = pattern.search(haystack)
            if hit:
                yield (label, number, f"denylisted {kind} {hit.group(0)!r}")


def decode(data: bytes) -> str | None:
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def scan_blob(label: str, data: bytes, terms: list[tuple[str, Pattern[str]]]) -> Iterator[Finding]:
    yield from scan_text(label + " (path)", label, terms)
    if data[:2] == b"PK":  # docx/pptx/xlsx and other zip containers: scan text members
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for member in archive.namelist():
                    if member.endswith((".xml", ".rels", ".txt", ".json")):
                        text = archive.read(member).decode("utf-8", errors="replace")
                        yield from scan_text(f"{label}!{member}", text, terms)
        except zipfile.BadZipFile:
            pass
        return
    text = decode(data)
    if text is not None:
        yield from scan_text(label, text, terms)


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def staged_files() -> list[str]:
    out = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    return [name for name in out.decode("utf-8").split("\0") if name]


def identity_findings(terms: list[tuple[str, Pattern[str]]]) -> Iterator[Finding]:
    for variable in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        try:
            ident = git("var", variable).decode("utf-8").strip()
        except subprocess.CalledProcessError:
            yield ("git identity", 0, f"{variable} is not set; configure user.name/user.email")
            continue
        person = ident.rsplit(">", 1)[0] + ">"
        yield from scan_text(f"git {variable}", person, terms)


def run_staged(terms: list[tuple[str, Pattern[str]]]) -> Iterator[Finding]:
    yield from identity_findings(terms)
    for name in staged_files():
        data = git("show", f":{name}")
        if len(data) <= MAX_BYTES:
            yield from scan_blob(name, data, terms)


def run_all(terms: list[tuple[str, Pattern[str]]]) -> Iterator[Finding]:
    tracked = git("ls-files", "-z").decode("utf-8").split("\0")
    untracked = git("ls-files", "-o", "--exclude-standard", "-z").decode("utf-8").split("\0")
    root = Path(git("rev-parse", "--show-toplevel").decode("utf-8").strip())
    for name in sorted({n for n in tracked + untracked if n}):
        path = root / name
        if path.is_file() and path.stat().st_size <= MAX_BYTES:
            yield from scan_blob(name, path.read_bytes(), terms)


def run_history(terms: list[tuple[str, Pattern[str]]]) -> Iterator[Finding]:
    """Commit metadata and messages, then every blob ever committed (zips unpacked)."""
    try:
        meta = git("log", "--all", "--format=commit %H%n%an <%ae>%n%cn <%ce>%n%B")
    except subprocess.CalledProcessError:
        return  # no commits yet
    yield from scan_text("git metadata", meta.decode("utf-8", errors="replace"), terms)
    listing = git("rev-list", "--all", "--objects").decode("utf-8", errors="replace")
    named: dict[str, str] = {}
    for line in listing.splitlines():
        sha, _, path = line.partition(" ")
        if path and sha not in named:
            named[sha] = path
    if not named:
        return
    checked = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objecttype) %(objectname)"],
        input="\n".join(named).encode(),
        capture_output=True,
        check=True,
    ).stdout.decode()
    for row in checked.splitlines():
        kind, _, sha = row.partition(" ")
        if kind == "blob":
            data = git("cat-file", "blob", sha)
            if len(data) <= MAX_BYTES:
                yield from scan_blob(f"history:{named[sha]}@{sha[:8]}", data, terms)


def report(findings: Iterable[Finding]) -> int:
    count = 0
    for label, line, message in findings:
        count += 1
        location = f"{label}:{line}" if line else label
        sys.stderr.write(f"denylist: {location}: {message}\n")
    if count:
        sys.stderr.write(
            f"denylist: {count} finding(s). Remove the personal or project-specific data, "
            f"or add '{SKIP_MARK}' to a line that is a false positive.\n"
        )
    return 1 if count else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--all", action="store_true")
    mode.add_argument("--history", action="store_true")
    mode.add_argument("--message", type=Path)
    mode.add_argument("--text", help="scan a literal string (for tests)")
    args = parser.parse_args(argv)

    path = denylist_path()
    terms = load_terms(path)
    if not terms:
        sys.stderr.write(f"denylist: no personal denylist at {path}; only built-in checks ran\n")
    if args.staged:
        findings: Iterable[Finding] = run_staged(terms)
    elif args.all:
        findings = run_all(terms)
    elif args.history:
        findings = run_history(terms)
    elif args.message is not None:
        message = args.message.read_text(encoding="utf-8", errors="replace")
        body = "\n".join(line for line in message.splitlines() if not line.startswith("#"))
        findings = scan_text("commit message", body, terms)
    else:
        findings = scan_text("text", args.text or "", terms)
    return report(findings)


if __name__ == "__main__":
    sys.exit(main())
