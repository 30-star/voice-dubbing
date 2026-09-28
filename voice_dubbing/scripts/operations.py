"""Pure sparse text editing over a reviewed, immutable base snapshot."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from ..errors import ValidationError
from ..models import ScriptVariant, TranscriptSegment, TranscriptTimeline
from ..models.correction import CorrectedTimeline
from ..models.script import BaseTimelineReference, timeline_structure_sha256, validate_script_binding
from ..models.transcript import _nonempty, timeline_sha256


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def create_script(base: CorrectedTimeline, *, base_path: Path, id: str, name: str) -> ScriptVariant:
    if not isinstance(base, CorrectedTimeline):
        raise ValidationError("script requires a reviewed CorrectedTimeline base")
    now = utc_now()
    script = ScriptVariant(id, name, BaseTimelineReference(base_path, base.raw_timeline_sha256,
                           timeline_structure_sha256(base.to_timeline())), {}, now, now)
    validate_script_binding(script, base)
    return script


def copy_script(script, *, id, name):
    _nonempty(id, "script id")
    if id.casefold() == script.id.casefold():
        raise ValidationError("a copied script requires a new variant id")
    now = utc_now()
    return replace(script, id=id, name=name, created_at=now, updated_at=now)


def _updated(script, overrides):
    return script if dict(script.text_overrides) == overrides else replace(
        script, text_overrides=overrides, updated_at=utc_now())


def set_segment_text(script, segment_id, text, *, base):
    validate_script_binding(script, base)
    _nonempty(text, "script text")
    original = next((s for s in base.segments if s.id == segment_id), None)
    if original is None:
        raise ValidationError(f"unknown script segment id: {segment_id}")
    overrides = dict(script.text_overrides)
    if text == original.text:
        overrides.pop(segment_id, None)
    else:
        overrides[segment_id] = text
    return _updated(script, overrides)


def restore_script_segment(script, segment_id, *, base):
    validate_script_binding(script, base)
    if segment_id not in {s.id for s in base.segments}:
        raise ValidationError(f"unknown script segment id: {segment_id}")
    overrides = dict(script.text_overrides)
    overrides.pop(segment_id, None)
    return _updated(script, overrides)


def rename_script(script, name):
    _nonempty(name, "script name")
    return script if name == script.name else replace(script, name=name, updated_at=utc_now())


def replace_script_text(script, find, replacement, *, base):
    if not isinstance(find, str) or not find:
        raise ValidationError("find must not be empty")
    if not isinstance(replacement, str):
        raise ValidationError("replacement must be a string")
    effective = resolve_script(base, script)
    count = sum(s.text.count(find) for s in effective.segments)
    if not count:
        return script, 0
    texts = {s.id: s.text.replace(find, replacement) for s in effective.segments}
    for text in texts.values():
        _nonempty(text, "script text")
    overrides = dict(script.text_overrides)
    for original, current in zip(base.segments, effective.segments):
        text = texts[current.id]
        if text == current.text:
            continue  # Do not discard an unchanged override just because the base caught up.
        if text == original.text:
            overrides.pop(current.id, None)
        else:
            overrides[current.id] = text
    return _updated(script, overrides), count


def resolve_script(base, script):
    validate_script_binding(script, base)
    return TranscriptTimeline(tuple(TranscriptSegment(s.id, s.start_ms, s.end_ms,
        script.text_overrides.get(s.id, s.text)) for s in base.segments), base.duration_ms)


def inspect_script(base, script):
    effective = resolve_script(base, script)
    rows = [{"id": s.id, "start_ms": s.start_ms, "end_ms": s.end_ms,
             "original_text": s.text, "text": e.text, "changed": s.text != e.text,
             "text_source": "override" if s.id in script.text_overrides else "base"}
            for s, e in zip(base.segments, effective.segments)]
    return {"id": script.id, "name": script.name, "schema_version": 2,
            "raw_timeline_sha256": base.raw_timeline_sha256,
            "base_timeline_sha256": timeline_sha256(base.to_timeline()),
            "effective_timeline_sha256": timeline_sha256(effective),
            "changed_segments": sum(r["changed"] for r in rows), "segments": rows}
