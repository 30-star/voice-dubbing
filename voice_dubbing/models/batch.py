"""Contracts for one transcription shared by several independent voices."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..errors import ValidationError
from .transcript import TranscriptTimeline, _nonempty


@dataclass(frozen=True, slots=True)
class VoiceProfile:
    id: str
    name: str
    provider: str
    voice_id: str
    model_id: str

    def __post_init__(self) -> None:
        for field in ("id", "name", "provider", "voice_id", "model_id"):
            _nonempty(getattr(self, field), field)
        if self.voice_id == "auto":
            raise ValidationError("batch voices must use explicit voice_id values")


def validate_voice_profiles(voices: tuple[VoiceProfile, ...]) -> tuple[VoiceProfile, ...]:
    try:
        voices = tuple(voices)
    except TypeError as exc:
        raise ValidationError("voices must be a sequence") from exc
    if not voices or any(not isinstance(voice, VoiceProfile) for voice in voices):
        raise ValidationError("voices must contain at least one VoiceProfile")
    if len({voice.id.casefold() for voice in voices}) != len(voices):
        raise ValidationError("duplicate voice profile id")
    if len({(voice.provider.casefold(), voice.voice_id) for voice in voices}) != len(voices):
        raise ValidationError("duplicate provider and voice_id")
    return voices


@dataclass(frozen=True, slots=True)
class BatchDubbingJob:
    source_video: Path
    timeline: TranscriptTimeline
    voices: tuple[VoiceProfile, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source_video, Path):
            raise ValidationError("source_video must be a Path")
        if not isinstance(self.timeline, TranscriptTimeline):
            raise ValidationError("timeline must be a TranscriptTimeline")
        object.__setattr__(self, "voices", validate_voice_profiles(self.voices))


@dataclass(frozen=True, slots=True)
class VoiceDubbingResult:
    voice: VoiceProfile
    status: Literal["succeeded", "failed"]
    audio_path: Path | None = None
    video_path: Path | None = None
    warnings: tuple[str, ...] = ()
    error: str | None = None
    output_duration_ms: int | None = None
    video_duration_ms: int | None = None
    cache_hits: int = 0
    supplier_requests: int = 0
    generated_segments: int = 0
    segment_cache: tuple[dict, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.voice, VoiceProfile):
            raise ValidationError("voice must be a VoiceProfile")
        if self.status == "succeeded":
            if self.audio_path is None or self.video_path is None or self.error is not None:
                raise ValidationError("succeeded voice requires audio and video paths without error")
        elif self.status == "failed":
            _nonempty(self.error, "error")
            if self.video_path is not None:
                raise ValidationError("failed voice cannot have a published video")
        else:
            raise ValidationError("status must be succeeded or failed")


@dataclass(frozen=True, slots=True)
class BatchDubbingResult:
    job: BatchDubbingJob
    status: Literal["succeeded", "partial_success", "failed"]
    voices: tuple[VoiceDubbingResult, ...]
    report_path: Path
    asr_calls: int
