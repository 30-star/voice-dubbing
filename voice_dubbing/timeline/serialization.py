"""Strict JSON boundary for transcript timelines."""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..errors import ValidationError
from ..models import TranscriptSegment, TranscriptTimeline


def _fields(value: Any, expected: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be a JSON object")
    missing = expected - value.keys()
    extra = value.keys() - expected
    if missing or extra:
        raise ValidationError(
            f"{name} fields mismatch: missing={sorted(missing)}, unexpected={sorted(extra)}"
        )
    return value


def timeline_from_dict(value: Any) -> TranscriptTimeline:
    data = _fields(value, {"segments", "duration_ms"}, "timeline")
    if not isinstance(data["segments"], list):
        raise ValidationError("segments must be a JSON array")
    segments = []
    for index, item in enumerate(data["segments"]):
        fields = _fields(item, {"id", "start_ms", "end_ms", "text"}, f"segment {index}")
        segments.append(TranscriptSegment(**fields))
    return TranscriptTimeline(segments=tuple(segments), duration_ms=data["duration_ms"])


def timeline_to_dict(timeline: TranscriptTimeline) -> dict[str, Any]:
    if not isinstance(timeline, TranscriptTimeline):
        raise ValidationError("timeline must be a TranscriptTimeline")
    return {
        "segments": [asdict(segment) for segment in timeline.segments],
        "duration_ms": timeline.duration_ms,
    }


def load_timeline(path: Path) -> TranscriptTimeline:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read timeline JSON at {path}: {exc}") from exc
    return timeline_from_dict(value)


def save_timeline(timeline: TranscriptTimeline, path: Path) -> None:
    data = json.dumps(timeline_to_dict(timeline), ensure_ascii=False, indent=2) + "\n"
    path.write_text(data, encoding="utf-8")
