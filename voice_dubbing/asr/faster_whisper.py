"""Local Faster-Whisper adapter; imported only when ASR is requested."""

import math
from pathlib import Path
from typing import Any

from ..errors import ProviderError
from ..models import TranscriptSegment, TranscriptTimeline


def _to_ms(seconds: Any, field: str) -> int:
    try:
        value = float(seconds)
    except (TypeError, ValueError) as exc:
        raise ProviderError(f"Faster-Whisper returned invalid {field}: {seconds!r}") from exc
    if not math.isfinite(value) or value < 0:
        raise ProviderError(f"Faster-Whisper returned invalid {field}: {seconds!r}")
    return round(value * 1000)


class FasterWhisperASRProvider:
    provider_id = "faster-whisper"

    def __init__(
        self,
        *,
        model_size: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        local_files_only: bool = False,
    ) -> None:
        if not model_size.strip():
            raise ValueError("model_size must not be empty")
        if device not in {"cpu", "cuda", "auto"}:
            raise ValueError("device must be cpu, cuda, or auto")
        if not compute_type.strip():
            raise ValueError("compute_type must not be empty")
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.local_files_only = local_files_only
        self._model: Any = None

    @property
    def cache_identity(self) -> dict:
        from importlib.metadata import version, PackageNotFoundError
        from .cache import file_sha256
        try:
            tool_version = version("faster-whisper")
        except PackageNotFoundError:
            tool_version = "unavailable"
        local = Path(self.model_size)
        model_files = ({str(p.relative_to(local)): file_sha256(p) for p in sorted(local.rglob("*")) if p.is_file()}
                       if local.is_dir() else None)
        return {"model": self.model_size, "local_model_files": model_files, "device": self.device,
                "compute_type": self.compute_type, "tool_version": tool_version,
                "beam_size": 5, "vad_filter": False, "adapter_version": 1}

    def transcribe(
        self, audio_path: Path, *, language: str | None = None
    ) -> TranscriptTimeline:
        if not audio_path.is_file():
            raise ProviderError(f"audio file not found: {audio_path}")
        if language == "auto":
            language = None
        try:
            if self._model is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as exc:
                    raise ProviderError(
                        "Faster-Whisper is not installed; run: python -m pip install -e '.[asr]'"
                    ) from exc
                self._model = WhisperModel(
                    self.model_size,
                    device=self.device,
                    compute_type=self.compute_type,
                    local_files_only=self.local_files_only,
                )
            segments, info = self._model.transcribe(
                str(audio_path), language=language, beam_size=5, vad_filter=False
            )
            duration_ms = _to_ms(info.duration, "duration")
            transcript = []
            for raw in segments:  # Faster-Whisper does the work while iterating.
                text = str(raw.text).strip()
                if not text:
                    continue
                start_ms = _to_ms(raw.start, "segment start")
                end_ms = _to_ms(raw.end, "segment end")
                if end_ms <= start_ms:
                    raise ProviderError("Faster-Whisper returned a segment with no duration")
                transcript.append(
                    TranscriptSegment(str(len(transcript) + 1), start_ms, end_ms, text)
                )
                duration_ms = max(duration_ms, end_ms)
            return TranscriptTimeline(transcript, duration_ms)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"Faster-Whisper transcription failed: {exc}") from exc
