"""Speech recognition and diarization backends.

ASR: MLX Whisper on Apple Silicon (fast, Metal), faster-whisper (CTranslate2)
everywhere else, including the future Windows port. Both return words with
timestamps, and both score which language is spoken, so a recording's language
can be detected before it is transcribed (see :func:`detect_language`).
Diarization: pyannote; its pipeline also yields one embedding per speaker, which
speaker suggestions reuse.

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

    def language_probs(self, samples: Any) -> dict[str, float]:
        """Probability of each language code for up to 30 s of 16 kHz mono samples."""
        ...


@dataclass(frozen=True)
class LanguageGuess:
    language: str  # the most likely of the candidate languages
    probability: float  # its share of the candidates' probability
    heard: str  # the most likely language overall
    heard_probability: float


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

    def language_probs(self, samples: Any) -> dict[str, float]:
        try:
            import mlx.core as mx
            from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim
            from mlx_whisper.transcribe import ModelHolder
        except ImportError as exc:
            raise MeetDependencyError("MLX Whisper needs the meet extra on Apple Silicon") from exc
        model = ModelHolder.get_model(self.model_id, mx.float16)  # cached for transcribe()
        if not model.is_multilingual:
            return {"en": 1.0}
        mel = log_mel_spectrogram(samples, n_mels=model.dims.n_mels, padding=N_SAMPLES)
        _tokens, probs = model.detect_language(
            pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16)
        )
        return {str(code): float(p) for code, p in probs.items()}

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
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise MeetDependencyError("faster-whisper needs the meet extra") from exc
            self._model = WhisperModel(self.model_id, device="auto", compute_type="int8")
        return self._model

    def language_probs(self, samples: Any) -> dict[str, float]:
        model = self._load()
        if not model.model.is_multilingual:
            return {"en": 1.0}
        _language, _probability, probs = model.detect_language(
            audio=samples, language_detection_segments=1
        )
        return {str(code): float(p) for code, p in probs}

    def transcribe(self, audio: Path, *, language: str | None, prompt: str | None) -> list[Word]:
        model = self._load()
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


_RATE = 16_000
_WINDOW = 30 * _RATE  # Whisper scores languages on 30-second windows


def speech_windows(samples: Any, *, count: int = 3) -> list[Any]:
    """Up to ``count`` 30-second windows with speech, one from each part of the
    recording (so a meeting that opens with small talk in another language still
    gets a say from its middle and end).

    Each part contributes its loudest window (the audio is loudness-normalized,
    so that is the one with most speech). Windows much quieter than the loudest
    are left out: Whisper tends to call near-silence English.
    """
    import numpy as np

    if len(samples) <= _WINDOW:
        return [samples]
    energy = np.concatenate([[0.0], np.cumsum(np.square(samples, dtype=np.float64))])

    def loudness(start: int) -> float:
        return float(energy[start + _WINDOW] - energy[start])

    parts = max(1, min(count, len(samples) // _WINDOW))
    part_length = len(samples) // parts
    hop = 5 * _RATE
    chosen: list[int] = []
    for part in range(parts):
        low = part * part_length
        high = min(low + part_length, len(samples)) - _WINDOW
        starts = [*range(low, high + 1, hop), high]
        chosen.append(max(starts, key=loudness))
    loudest = max(loudness(start) for start in chosen)
    voiced = [start for start in chosen if loudness(start) >= 0.1 * loudest]
    return [samples[start : start + _WINDOW] for start in voiced]


def detect_language(
    asr: AsrBackend, audio: Path, candidates: list[str] | None = None
) -> LanguageGuess:
    """Detect the spoken language from a few speech windows across the recording.

    With ``candidates`` (the workspace's languages), the answer is the likeliest
    of those: Whisper sometimes hears Spanish as Portuguese or Galician on short
    or accented audio, and restricting the choice avoids that.
    """
    totals: dict[str, float] = {}
    for window in speech_windows(_wav_samples(audio)):
        for code, probability in asr.language_probs(window).items():
            totals[code] = totals.get(code, 0.0) + probability
    if not totals:
        raise DossierError("language detection returned nothing; set the language explicitly")
    heard = max(totals, key=lambda code: totals[code])
    overall = sum(totals.values()) or 1.0
    pool = {code: totals.get(code, 0.0) for code in candidates} if candidates else totals
    best = max(pool, key=lambda code: pool[code])
    share = pool[best] / (sum(pool.values()) or 1.0)
    return LanguageGuess(
        language=best,
        probability=round(share, 3),
        heard=heard,
        heard_probability=round(totals[heard] / overall, 3),
    )


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
