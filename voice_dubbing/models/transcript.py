"""Provider-independent transcript timeline measured in integer milliseconds."""

import hashlib
import json
from dataclasses import asdict, dataclass

from ..errors import ValidationError


def _nonempty(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a non-empty string")


def _milliseconds(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValidationError(f"{name} must be a non-negative integer in milliseconds")


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    id: str
    start_ms: int
    end_ms: int
    text: str

    def __post_init__(self) -> None:
        _nonempty(self.id, "segment id")
        _milliseconds(self.start_ms, "start_ms")
        _milliseconds(self.end_ms, "end_ms")
        if self.end_ms <= self.start_ms:
            raise ValidationError("end_ms must be greater than start_ms")
        _nonempty(self.text, "segment text")


@dataclass(frozen=True, slots=True)
class TranscriptTimeline:
    segments: tuple[TranscriptSegment, ...]
    duration_ms: int

    def __post_init__(self) -> None:
        _milliseconds(self.duration_ms, "duration_ms")
        try:
            segments = tuple(self.segments)
        except TypeError as exc:
            raise ValidationError("segments must be a sequence") from exc
        if any(not isinstance(segment, TranscriptSegment) for segment in segments):
            raise ValidationError("segments must contain TranscriptSegment values")
        seen: set[str] = set()
        previous_start = -1
        for segment in segments:
            if segment.id in seen:
                raise ValidationError(f"duplicate segment id: {segment.id}")
            if segment.start_ms < previous_start:
                raise ValidationError("segments must be sorted by start_ms")
            if segment.end_ms > self.duration_ms:
                raise ValidationError(f"segment {segment.id} ends after duration_ms")
            seen.add(segment.id)
            previous_start = segment.start_ms
        object.__setattr__(self, "segments", segments)


def timeline_sha256(timeline: TranscriptTimeline) -> str:
    """Stable identity including original text, IDs, timing and total duration."""
    if not isinstance(timeline, TranscriptTimeline):
        raise ValidationError("timeline must be a TranscriptTimeline")
    data = {"segments": [asdict(segment) for segment in timeline.segments],
            "duration_ms": timeline.duration_ms}
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
