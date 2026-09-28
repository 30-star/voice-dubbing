"""Provider-neutral text-to-speech input and produced audio metadata."""

from dataclasses import dataclass
from pathlib import Path

from ..errors import ValidationError
from .transcript import _milliseconds, _nonempty


@dataclass(frozen=True, slots=True)
class SpeechRequest:
    segment_id: str
    text: str
    voice_id: str
    language: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.segment_id, "segment_id")
        _nonempty(self.text, "text")
        _nonempty(self.voice_id, "voice_id")
        if self.language is not None:
            _nonempty(self.language, "language")


@dataclass(frozen=True, slots=True)
class SpeechAudio:
    segment_id: str
    audio_path: Path
    duration_ms: int
    sample_rate_hz: int
    channels: int

    def __post_init__(self) -> None:
        _nonempty(self.segment_id, "segment_id")
        if not isinstance(self.audio_path, Path):
            raise ValidationError("audio_path must be a Path")
        _nonempty(str(self.audio_path), "audio_path")
        _milliseconds(self.duration_ms, "duration_ms")
        if self.duration_ms == 0:
            raise ValidationError("duration_ms must be greater than zero")
        if type(self.sample_rate_hz) is not int or self.sample_rate_hz <= 0:
            raise ValidationError("sample_rate_hz must be a positive integer")
        if type(self.channels) is not int or self.channels <= 0:
            raise ValidationError("channels must be a positive integer")
