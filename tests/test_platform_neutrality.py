"""Everything outside ``dossier.platform`` must run on Windows unchanged."""

from __future__ import annotations

import pkgutil
import re
import subprocess
import sys
from pathlib import Path

import dossier

SRC = Path(dossier.__file__).parent

# Programs and APIs that exist only on macOS (or only on POSIX).
FORBIDDEN = re.compile(
    r"\b(osascript|launchctl|caffeinate|afconvert|ocrmac|st_flags|fcntl|plistlib|"
    r"os\.getuid|os\.fork|LaunchAgents)\b|\[\"say\"|'say'"
)


def _modules() -> list[str]:
    return sorted(
        info.name
        for info in pkgutil.walk_packages([str(SRC)], prefix="dossier.")
        if not info.name.startswith("dossier.platform.")
    )


def test_no_os_specific_calls_outside_platform() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        if "platform" in path.relative_to(SRC).parts[:1]:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")
    assert not offenders, "OS-specific code outside dossier/platform:\n" + "\n".join(offenders)


def test_every_module_imports_as_windows() -> None:
    modules = _modules()
    assert "dossier.cli" in modules
    # Load the standard library and third-party dependencies first: they have
    # their own Windows branches that need Windows-only builtins. The point is
    # dossier's own code paths.
    preload = [
        "shutil",
        "zipfile",
        "subprocess",
        "tempfile",
        "socket",
        "importlib.metadata",
        "asyncio",
        "httpx",
        "pydantic",
        "rich.console",
        "rich.logging",
        "rich.table",
        "typer",
        "typer.testing",
        "platformdirs",
        "tomlkit",
        "tomllib",
        "keyring",
        "platform",
        "sysconfig",
    ]
    script = (
        "import sys, importlib\n"
        f"for name in {preload!r}:\n"
        "    importlib.import_module(name)\n"
        "import sysconfig; sysconfig.get_config_vars()\n"
        "sys.platform = 'win32'\n"
        f"modules = {modules!r}\n"
        "for name in modules:\n"
        "    importlib.import_module(name)\n"
        "from dossier import platform\n"
        "assert platform.current().name == 'windows', platform.current().name\n"
        "print('ok', len(modules))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("ok")
