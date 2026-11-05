"""Speech recognition and diarization backends.

ASR: MLX Whisper on Apple Silicon (fast, Metal), faster-whisper (CTranslate2)
everywhere else, including the future Windows port. Both return words with
timestamps. Diarization: pyannote; its pipeline also yields one embedding per
speaker, which speaker suggestions reuse.

Backends are small classes behind protocols so tests (and other engines) can
replace them.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import platform as stdlib_platform
import re
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from dossier.core.config import AsrBackendName, MeetSection
from dossier.core.errors import ConfigError, DossierError
from dossier.core.secrets import get_secret
from dossier.meet.models import DiarSegment, Word

log = logging.getLogger("dossier.meet")

DEFAULT_MLX_MODEL = "mlx-community/whisper-large-v3-turbo-4bit"
DEFAULT_FASTER_WHISPER_MODEL = "large-v3-turbo"


class MeetDependencyError(DossierError):
    code = "meet_extra_missing"
    exit_status = 69


class AsrBackend(Protocol):
    name: str
    model_id: str

    def transcribe(
        self, audio: Path, *, language: str | None, prompt: str | None
    ) -> list[Word]: ...


@dataclass
class Diarization:
    segments: list[DiarSegment]
    embeddings: dict[str, list[float]] = field(default_factory=dict)


class Diarizer(Protocol):
    name: str

    def diarize(self, audio: Path) -> Diarization: ...


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class MlxWhisperBackend:
    name = "mlx-whisper"

    def __init__(self, model: str | None = None) -> None:
        self.model_id = model or DEFAULT_MLX_MODEL

    def transcribe(self, audio: Path, *, language: str | None, prompt: str | None) -> list[Word]:
        try:
            import mlx_whisper
        except ImportError as exc:
            raise MeetDependencyError("MLX Whisper needs the meet extra on Apple Silicon") from exc
        result: dict[str, Any] = mlx_whisper.transcribe(
            str(audio),
            path_or_hf_repo=self.model_id,
            language=language,
            task="transcribe",
            word_timestamps=True,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.4,
            logprob_threshold=-1.0,
            initial_prompt=prompt,
            verbose=None,
        )
        words: list[Word] = []
        for segment in result.get("segments", []):
            for raw in segment.get("words", []):
                text = _clean(str(raw.get("word", "")))
                if text:
                    words.append(
                        Word(
                            start=float(raw["start"]),
                            end=float(raw["end"]),
                            text=text,
                            confidence=_float(raw.get("probability")),
                        )
                    )
        return words


class FasterWhisperBackend:
    name = "faster-whisper"

    def __init__(self, model: str | None = None) -> None:
        self.model_id = model or DEFAULT_FASTER_WHISPER_MODEL

    def transcribe(self, audio: Path, *, language: str | None, prompt: str | None) -> list[Word]:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise MeetDependencyError("faster-whisper needs the meet extra") from exc
        model = WhisperModel(self.model_id, device="auto", compute_type="int8")
        # Pass samples, not a path: our audio is already 16 kHz mono, and this skips
        # faster-whisper's PyAV decoder (whose arguments drift between PyAV releases).
        segments, _info = model.transcribe(
            _wav_samples(audio),
            language=language,
            initial_prompt=prompt,
            word_timestamps=True,
            condition_on_previous_text=False,
            vad_filter=True,
        )
        words: list[Word] = []
        for segment in segments:
            for raw in segment.words or []:
                text = _clean(raw.word)
                if text:
                    words.append(
                        Word(
                            start=float(raw.start),
                            end=float(raw.end),
                            text=text,
                            confidence=_float(raw.probability),
                        )
                    )
        return words


def _apple_silicon() -> bool:
    return sys.platform == "darwin" and stdlib_platform.machine() == "arm64"


def asr_backend(settings: MeetSection) -> AsrBackend:
    choice = settings.asr_backend
    if choice is AsrBackendName.AUTO:
        if _apple_silicon() and importlib.util.find_spec("mlx_whisper") is not None:
            choice = AsrBackendName.MLX
        else:
            choice = AsrBackendName.FASTER_WHISPER
    if choice is AsrBackendName.MLX:
        return MlxWhisperBackend(settings.asr_model)
    return FasterWhisperBackend(settings.asr_model)


def meet_extra_available() -> bool:
    return importlib.util.find_spec("pyannote") is not None and (
        importlib.util.find_spec("mlx_whisper") is not None
        or importlib.util.find_spec("faster_whisper") is not None
    )


class PyannoteDiarizer:
    """pyannote speaker diarization; returns segments and per-speaker embeddings.

    The model is gated on Hugging Face: accept its terms once, then store a token
    with ``dsr secrets set hf-token``. Without a token, a model already in the
    local Hugging Face cache is used offline.
    """

    name = "pyannote"

    def __init__(self, model: str) -> None:
        self.model = model

    def diarize(self, audio: Path) -> Diarization:
        token = get_secret("hf-token") or os.environ.get("HF_TOKEN")
        if not token:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")  # use the local cache only
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                import torch
                from pyannote.audio import Pipeline
        except ImportError as exc:
            raise MeetDependencyError("diarization needs the meet extra") from exc
        try:
            pipeline = Pipeline.from_pretrained(self.model, token=token)
        except Exception as exc:
            raise ConfigError(
                f"cannot load {self.model} ({type(exc).__name__}). Accept the model's terms on "
                "Hugging Face, then run `dsr secrets set hf-token`."
            ) from exc
        if pipeline is None:
            raise ConfigError(f"cannot load diarization model {self.model}")
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        pipeline.to(torch.device(device))
        waveform = _load_wav(audio, torch)
        output = pipeline({"waveform": waveform, "sample_rate": 16_000})
        annotation: Any = getattr(output, "speaker_diarization", output)
        segments = [
            DiarSegment(start=float(turn.start), end=float(turn.end), speaker=str(label))
            for turn, _track, label in annotation.itertracks(yield_label=True)
        ]
        embeddings: dict[str, list[float]] = {}
        raw = getattr(output, "speaker_embeddings", None)
        if raw is not None:
            labels = [str(label) for label in annotation.labels()]
            for label, vector in zip(labels, raw, strict=False):
                values = [float(x) for x in vector]
                if values and all(v == v for v in values):  # skip NaN rows
                    embeddings[label] = values
        return Diarization(segments=segments, embeddings=embeddings)


def _wav_samples(path: Path) -> Any:
    """16-bit PCM WAV -> float32 numpy array in [-1, 1]."""
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def _load_wav(path: Path, torch: Any) -> Any:
    return torch.from_numpy(_wav_samples(path)).unsqueeze(0)


class SingleSpeaker:
    """Used when diarization is turned off: everything is SPEAKER_00."""

    name = "none"

    def diarize(self, audio: Path) -> Diarization:
        del audio
        return Diarization(segments=[])
