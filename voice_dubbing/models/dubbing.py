"""Task and per-voice result contracts, independent of an executor."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..errors import ValidationError
from .transcript import TranscriptTimeline, _nonempty

VariantStatus = Literal["succeeded", "failed", "cancelled"]


def _path(value: object, name: str) -> None:
    if not isinstance(value, Path) or not str(value).strip():
        raise ValidationError(f"{name} must be a Path")


@dataclass(frozen=True, slots=True)
class VoiceSelection:
    provider_id: str
    voice_id: str

    def __post_init__(self) -> None:
        _nonempty(self.provider_id, "provider_id")
        _nonempty(self.voice_id, "voice_id")


@dataclass(frozen=True, slots=True)
class DubbingTask:
    task_id: str
    input_video: Path
    output_dir: Path
    voices: tuple[VoiceSelection, ...]
    language: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.task_id, "task_id")
        _path(self.input_video, "input_video")
        _path(self.output_dir, "output_dir")
        try:
            voices = tuple(self.voices)
        except TypeError as exc:
            raise ValidationError("voices must be a sequence") from exc
        if not voices or any(not isinstance(voice, VoiceSelection) for voice in voices):
            raise ValidationError("voices must contain at least one VoiceSelection")
        if len(set(voices)) != len(voices):
            raise ValidationError("voices must not contain duplicates")
        if self.language is not None:
            _nonempty(self.language, "language")
        object.__setattr__(self, "voices", voices)


@dataclass(frozen=True, slots=True)
class DubbingVariantResult:
    voice: VoiceSelection
    status: VariantStatus
    audio_path: Path | None = None
    video_path: Path | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.voice, VoiceSelection):
            raise ValidationError("voice must be a VoiceSelection")
        if self.status not in ("succeeded", "failed", "cancelled"):
            raise ValidationError("status must be succeeded, failed, or cancelled")
        if self.audio_path is not None:
            _path(self.audio_path, "audio_path")
        if self.video_path is not None:
            _path(self.video_path, "video_path")
        if self.status == "succeeded":
            if self.video_path is None or self.error is not None:
                raise ValidationError("a succeeded variant requires video_path and no error")
        elif self.status == "failed":
            _nonempty(self.error, "error")
            if self.video_path is not None:
                raise ValidationError("a failed variant cannot have video_path")
        elif self.error is not None or self.video_path is not None:
            raise ValidationError("a cancelled variant cannot have error or video_path")


@dataclass(frozen=True, slots=True)
class DubbingResult:
    task_id: str
    timeline: TranscriptTimeline | None
    variants: tuple[DubbingVariantResult, ...]

    def __post_init__(self) -> None:
        _nonempty(self.task_id, "task_id")
        if self.timeline is not None and not isinstance(self.timeline, TranscriptTimeline):
            raise ValidationError("timeline must be a TranscriptTimeline or None")
        try:
            variants = tuple(self.variants)
        except TypeError as exc:
            raise ValidationError("variants must be a sequence") from exc
        if any(not isinstance(variant, DubbingVariantResult) for variant in variants):
            raise ValidationError("variants must contain DubbingVariantResult values")
        if len({variant.voice for variant in variants}) != len(variants):
            raise ValidationError("variants must have unique voices")
        object.__setattr__(self, "variants", variants)
