"""Human correction of the ASR baseline, independent of ScriptVariant."""

import re
from dataclasses import dataclass
from pathlib import Path

from ..errors import ValidationError
from .transcript import TranscriptSegment, TranscriptTimeline, _nonempty, timeline_sha256


def _sha256(value: object, name: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValidationError(f"{name} must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class CorrectedSegment:
    id: str
    start_ms: int
    end_ms: int
    original_text: str
    text: str

    def __post_init__(self) -> None:
        TranscriptSegment(self.id, self.start_ms, self.end_ms, self.text)
        _nonempty(self.original_text, "original_text")

    @property
    def edited(self) -> bool:
        return self.text != self.original_text


@dataclass(frozen=True, slots=True)
class CorrectedTimeline:
    raw_timeline_sha256: str
    duration_ms: int
    segments: tuple[CorrectedSegment, ...]
    reviewed: bool = False

    def __post_init__(self) -> None:
        _sha256(self.raw_timeline_sha256, "raw_timeline_sha256")
        if type(self.reviewed) is not bool:
            raise ValidationError("reviewed must be a boolean")
        try:
            segments = tuple(self.segments)
        except TypeError as exc:
            raise ValidationError("corrected segments must be a sequence") from exc
        if any(not isinstance(segment, CorrectedSegment) for segment in segments):
            raise ValidationError("corrected segments must contain CorrectedSegment values")
        object.__setattr__(self, "segments", segments)
        self.to_timeline()

    def to_timeline(self) -> TranscriptTimeline:
        return TranscriptTimeline(tuple(TranscriptSegment(segment.id, segment.start_ms, segment.end_ms,
                                                         segment.text) for segment in self.segments), self.duration_ms)

    def validate_raw(self, raw: TranscriptTimeline) -> None:
        if self.raw_timeline_sha256 != timeline_sha256(raw):
            raise ValidationError("corrected timeline does not match raw timeline fingerprint")
        original = [(s.id, s.start_ms, s.end_ms, s.text) for s in raw.segments]
        retained = [(s.id, s.start_ms, s.end_ms, s.original_text) for s in self.segments]
        if self.duration_ms != raw.duration_ms or original != retained:
            raise ValidationError("corrected IDs, count, order, times, duration and original_text must match raw")


@dataclass(frozen=True, slots=True)
class CorrectionDraft:
    base_corrected_sha256: str
    timeline: CorrectedTimeline

    def __post_init__(self) -> None:
        _sha256(self.base_corrected_sha256, "base_corrected_sha256")
        if not isinstance(self.timeline, CorrectedTimeline):
            raise ValidationError("draft timeline must be a CorrectedTimeline")


@dataclass(frozen=True, slots=True)
class AwaitingCorrection:
    timeline: TranscriptTimeline
    subtitle_dir: Path
    asr_calls: int = 1
    status: str = "awaiting_correction"
