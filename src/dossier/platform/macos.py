"""macOS backends: Apple Vision OCR, ``say``, ``afplay``, launchd, ``osascript``, ``caffeinate``.

This is the only module allowed to call macOS-specific programs and APIs.
"""

from __future__ import annotations

import importlib.util
import os
import plistlib
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from dossier.core.errors import CapabilityUnavailableError, ExternalToolError
from dossier.platform.base import OcrLine, Platform, ScheduledJob, ScheduleStatus
from dossier.platform.generic import PosixProcessProbe

# st_flags bit for APFS "dataless" files (iCloud Drive / File Provider stubs,
# including OneDrive Files On-Demand). Reading such a file triggers a download.
SF_DATALESS = 0x40000000


def _run(
    command: list[str], what: str, *, timeout: float = 300
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise ExternalToolError(f"{what}: {command[0]} not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise ExternalToolError(f"{what}: timed out after {timeout:.0f}s") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()[:500]
        raise ExternalToolError(f"{what} failed ({completed.returncode}): {detail}")
    return completed


class OsascriptNotifier:
    name = "notify"

    def available(self) -> bool:
        return shutil.which("osascript") is not None

    def detail(self) -> str:
        return "Notification Center via osascript"

    def notify(self, title: str, message: str) -> None:
        # Pass text as argv, never interpolated into the script, so quotes in a
        # filename cannot break out of the AppleScript string.
        script = [
            "osascript",
            "-e",
            "on run argv",
            "-e",
            "display notification (item 2 of argv) with title (item 1 of argv)",
            "-e",
            "end run",
            title[:120],
            message[:400],
        ]
        subprocess.run(script, check=False, capture_output=True, timeout=10)


class CaffeinateKeepAwake:
    name = "keep_awake"

    def available(self) -> bool:
        return shutil.which("caffeinate") is not None

    def detail(self) -> str:
        return "caffeinate -i"

    @contextmanager
    def hold(self, reason: str) -> Iterator[None]:
        del reason
        if not self.available():
            yield
            return
        # -w ties the assertion to our pid, so it ends even if we are killed.
        process = subprocess.Popen(
            ["caffeinate", "-i", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            yield
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


class DatalessPlaceholders:
    name = "cloud_placeholders"

    def available(self) -> bool:
        return True

    def detail(self) -> str:
        return "APFS dataless flag (iCloud Drive, OneDrive Files On-Demand)"

    def is_placeholder(self, path: Path) -> bool:
        try:
            info = path.stat(follow_symlinks=False)
        except OSError:
            return False
        flags = getattr(info, "st_flags", 0)
        return stat.S_ISREG(info.st_mode) and bool(flags & SF_DATALESS)


class AppleVisionOcr:
    name = "ocr"

    def available(self) -> bool:
        return importlib.util.find_spec("ocrmac") is not None

    def detail(self) -> str:
        if self.available():
            return "Apple Vision (ocrmac)"
        return "Apple Vision needs the ocr extra: uv tool install 'dossier[ocr]'"

    def recognize(  # pragma: no cover - needs the Vision framework
        self, image: Path, *, languages: list[str] | None = None
    ) -> list[OcrLine]:
        if not self.available():
            raise CapabilityUnavailableError(self.detail())
        from ocrmac import ocrmac
        from PIL import Image

        with Image.open(image) as picture:
            observations = ocrmac.OCR(
                picture, recognition_level="accurate", language_preference=languages
            ).recognize()
        lines: list[OcrLine] = []
        for text, confidence, bbox in observations:
            cleaned = str(text).strip()
            if not cleaned:
                continue
            x, y, width, height = (float(v) for v in bbox)
            # Vision's origin is bottom-left; convert to top-left.
            lines.append(OcrLine(cleaned, float(confidence), (x, 1.0 - y - height, width, height)))
        lines.sort(key=lambda line: (line.box[1], line.box[0]) if line.box else (0.0, 0.0))
        return lines


class SayTts:
    name = "tts"

    def available(self) -> bool:
        return shutil.which("say") is not None and shutil.which("afconvert") is not None

    def detail(self) -> str:
        return "say + afconvert"

    def voices(self) -> list[str]:
        completed = _run(["say", "-v", "?"], "listing voices", timeout=30)
        names: list[str] = []
        for line in completed.stdout.splitlines():
            # "Name    xx_YY    # sample"; names may contain spaces.
            head = line.split("#", 1)[0].rstrip()
            parts = head.rsplit(None, 1)
            if len(parts) == 2:
                names.append(parts[0].strip())
        return names

    def synthesize(self, text: str, output: Path, *, voice: str | None = None) -> None:
        if not self.available():
            raise CapabilityUnavailableError("say/afconvert not found")
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        suffix = output.suffix.lower()
        with tempfile.TemporaryDirectory(prefix="dossier-tts-") as temporary_name:
            temporary = Path(temporary_name)
            text_file = temporary / "speech.txt"
            text_file.write_text(text, encoding="utf-8")
            aiff = temporary / "speech.aiff"
            command = ["say", "-f", str(text_file), "-o", str(aiff)]
            if voice:
                command[1:1] = ["-v", voice]
            _run(command, "speech synthesis")
            if suffix == ".aiff":
                aiff.replace(output)
                return
            encoded = temporary / f"speech{suffix}"
            if suffix == ".wav":
                convert = ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", str(aiff), str(encoded)]
            elif suffix == ".m4a":
                convert = [
                    "afconvert",
                    "-f",
                    "m4af",
                    "-d",
                    "aac",
                    "-b",
                    "64000",
                    str(aiff),
                    str(encoded),
                ]
            else:
                raise ValueError(f"unsupported audio format {suffix!r}; use .wav, .m4a or .aiff")
            _run(convert, "audio encoding")
            encoded.replace(output)


class AfplayPlayer:
    name = "player"

    def available(self) -> bool:
        return shutil.which("afplay") is not None

    def detail(self) -> str:
        return "afplay"

    def play(self, path: Path) -> None:
        if not self.available():
            raise CapabilityUnavailableError("afplay not found")
        _run(["afplay", str(path)], "audio playback", timeout=600)


class LaunchdScheduler:
    name = "scheduler"

    def __init__(self, agents_dir: Path | None = None) -> None:
        self.agents_dir = agents_dir or Path.home() / "Library" / "LaunchAgents"

    def available(self) -> bool:
        return shutil.which("launchctl") is not None

    def detail(self) -> str:
        return f"launchd user agents in {self.agents_dir}"

    def _plist_path(self, label: str) -> Path:
        return self.agents_dir / f"{label}.plist"

    def _domain(self) -> str:
        return f"gui/{os.getuid()}"

    def render(self, job: ScheduledJob) -> bytes:
        definition: dict[str, object] = {
            "Label": job.label,
            "ProgramArguments": list(job.program),
            "WorkingDirectory": str(job.working_dir),
            "StandardOutPath": str(job.log_path),
            "StandardErrorPath": str(job.log_path),
            "RunAtLoad": False,
            "ProcessType": "Background",
            "LowPriorityIO": True,
        }
        if job.daily_at is not None:
            hour, minute = job.daily_at
            definition["StartCalendarInterval"] = {"Hour": hour, "Minute": minute}
        if job.watch_paths:
            definition["WatchPaths"] = [str(path) for path in job.watch_paths]
            definition["ThrottleInterval"] = 300
        if job.environment:
            definition["EnvironmentVariables"] = dict(job.environment)
        return plistlib.dumps(definition, fmt=plistlib.FMT_XML, sort_keys=True)

    def install(self, job: ScheduledJob) -> Path:
        path = self._plist_path(job.label)
        path.parent.mkdir(parents=True, exist_ok=True)
        job.log_path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            self.remove(job.label)
        path.write_bytes(self.render(job))
        _run(["launchctl", "bootstrap", self._domain(), str(path)], "launchctl bootstrap")
        return path

    def remove(self, label: str) -> bool:
        path = self._plist_path(label)
        subprocess.run(
            ["launchctl", "bootout", f"{self._domain()}/{label}"],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if path.exists():
            path.unlink()
            return True
        return False

    def status(self, label: str) -> ScheduleStatus:
        path = self._plist_path(label)
        completed = subprocess.run(
            ["launchctl", "print", f"{self._domain()}/{label}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        loaded = completed.returncode == 0
        detail = "loaded" if loaded else ("plist present, not loaded" if path.exists() else "")
        return ScheduleStatus(label=label, installed=path.exists() and loaded, detail=detail)


def build() -> Platform:
    return Platform(
        name="macos",
        process=PosixProcessProbe(),
        notifier=OsascriptNotifier(),
        keep_awake=CaffeinateKeepAwake(),
        placeholders=DatalessPlaceholders(),
        ocr=AppleVisionOcr(),
        tts=SayTts(),
        scheduler=LaunchdScheduler(),
        player=AfplayPlayer(),
    )
