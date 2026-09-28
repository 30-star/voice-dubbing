"""Strict correction JSON and single-file atomic publication."""

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from ..errors import ValidationError
from ..models.correction import CorrectedSegment, CorrectedTimeline, CorrectionDraft
from ..timeline.serialization import _fields


def corrected_to_dict(timeline: CorrectedTimeline) -> dict:
    return {"schema_version": 1, "raw_timeline_sha256": timeline.raw_timeline_sha256,
            "duration_ms": timeline.duration_ms, "reviewed": timeline.reviewed,
            "segments": [{**asdict(s), "edited": s.edited} for s in timeline.segments]}


def corrected_from_dict(value: object) -> CorrectedTimeline:
    data = _fields(value, {"schema_version", "raw_timeline_sha256", "duration_ms", "reviewed", "segments"},
                   "corrected timeline")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValidationError("unsupported correction schema_version")
    if not isinstance(data["segments"], list):
        raise ValidationError("corrected segments must be a JSON array")
    segments = []
    for item in data["segments"]:
        fields = dict(_fields(item, {"id", "start_ms", "end_ms", "original_text", "text", "edited"},
                              "corrected segment"))
        edited = fields.pop("edited")
        segment = CorrectedSegment(**fields)
        if type(edited) is not bool or edited != segment.edited:
            raise ValidationError("edited must reflect text != original_text")
        segments.append(segment)
    return CorrectedTimeline(data["raw_timeline_sha256"], data["duration_ms"], tuple(segments), data["reviewed"])


def corrected_sha256(timeline: CorrectedTimeline) -> str:
    encoded = json.dumps(corrected_to_dict(timeline), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read correction JSON at {path}: {exc}") from exc


def write_json_atomic(value: dict, path: Path, *, overwrite: bool = False) -> None:
    data = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".correction-", suffix=".partial", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    except FileExistsError as exc:
        raise ValidationError(f"correction output already exists: {path}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_corrected(path: Path) -> CorrectedTimeline:
    return corrected_from_dict(read_json(path))


def load_draft(path: Path) -> CorrectionDraft:
    fields = _fields(read_json(path), {"schema_version", "base_corrected_sha256", "timeline"}, "correction draft")
    if type(fields["schema_version"]) is not int or fields["schema_version"] != 1:
        raise ValidationError("unsupported draft schema_version")
    return CorrectionDraft(fields["base_corrected_sha256"], corrected_from_dict(fields["timeline"]))


def save_draft(draft: CorrectionDraft, path: Path) -> None:
    write_json_atomic({"schema_version": 1, "base_corrected_sha256": draft.base_corrected_sha256,
                       "timeline": corrected_to_dict(draft.timeline)}, path, overwrite=True)
