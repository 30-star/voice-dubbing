"""Immutable text-only edits to a correction baseline."""

from dataclasses import replace

from ..errors import ValidationError
from ..models.correction import CorrectedSegment, CorrectedTimeline
from ..models.transcript import TranscriptTimeline, timeline_sha256


def create_correction(raw: TranscriptTimeline) -> CorrectedTimeline:
    return CorrectedTimeline(timeline_sha256(raw), raw.duration_ms,
        tuple(CorrectedSegment(s.id, s.start_ms, s.end_ms, s.text, s.text) for s in raw.segments))


def set_text(timeline: CorrectedTimeline, segment_id: str, text: str) -> CorrectedTimeline:
    if segment_id not in {s.id for s in timeline.segments}:
        raise ValidationError(f"unknown correction segment id: {segment_id}")
    return replace(timeline, segments=tuple(replace(s, text=text) if s.id == segment_id else s
                                           for s in timeline.segments))


def restore_segment(timeline: CorrectedTimeline, segment_id: str) -> CorrectedTimeline:
    for segment in timeline.segments:
        if segment.id == segment_id:
            return set_text(timeline, segment_id, segment.original_text)
    raise ValidationError(f"unknown correction segment id: {segment_id}")


def restore_all(timeline: CorrectedTimeline) -> CorrectedTimeline:
    return replace(timeline, segments=tuple(replace(s, text=s.original_text) for s in timeline.segments))
