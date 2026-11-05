"""Audio normalization with ffmpeg: any input -> 16 kHz mono 16-bit WAV.

EBU loudness normalization (ffmpeg loudnorm) evens out quiet and loud speakers, which helps
both recognition and diarization. Video inputs keep only their first audio track.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import wave
from datetime import UTC, datetime
from pathlib import Path

from dossier.core.errors import ExternalToolError

SAMPLE_RATE = 16_000


def _ffmpeg(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise ExternalToolError(f"{name} not found; install ffmpeg (brew install ffmpeg)")
    return found


def normalize(source: Path, target: Path, *, loudnorm: bool = True) -> float:
    """Write ``target`` as 16 kHz mono PCM WAV; return its duration in seconds."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".part.wav")
    command = [
        _ffmpeg("ffmpeg"),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-map",
        "0:a:0",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
    ]
    if loudnorm:
        command += ["-af", "loudnorm=I=-23:TP=-2:LRA=11"]
    command += ["-c:a", "pcm_s16le", str(temporary)]
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=7200)
    if completed.returncode != 0:
        temporary.unlink(missing_ok=True)
        detail = (completed.stderr or "").strip()[:300]
        raise ExternalToolError(f"ffmpeg could not read the recording: {detail}")
    temporary.replace(target)
    return wav_duration(target)


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def recorded_date(source: Path) -> str:
    """Best guess of the meeting date: a date in the file name, the container's
    creation time, then the file's modification time."""
    match = re.search(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})", source.stem)
    if match:
        year, month, day = match.groups()
        if 1 <= int(month) <= 12 and 1 <= int(day) <= 31:
            return f"{year}-{month}-{day}"
    probe = shutil.which("ffprobe")
    if probe:
        completed = subprocess.run(
            [probe, "-v", "quiet", "-print_format", "json", "-show_format", str(source)],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        try:
            tags = json.loads(completed.stdout or "{}").get("format", {}).get("tags", {})
            created = tags.get("creation_time") or tags.get("date")
            if created:
                return str(created)[:10]
        except json.JSONDecodeError:
            pass
    return datetime.fromtimestamp(source.stat().st_mtime, UTC).date().isoformat()
