"""Strict UTF-8 variant files and atomic editing, separate from ASR JSON."""

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path

from ..errors import ValidationError
from ..models import ScriptDubbingSelection, ScriptSegment, ScriptVariant, VoiceProfile
from ..timeline.serialization import _fields
from ..models.script import BaseTimelineReference
from ..models.transcript import timeline_sha256
from .operations import create_script


def script_to_dict(script: ScriptVariant, *, destination: Path | None = None) -> dict:
    reference = str(script.base_timeline.path)
    if destination is not None:
        try:
            reference = os.path.relpath(script.base_timeline.path, destination.resolve().parent)
        except ValueError:
            pass  # Different Windows drives require an absolute reference.
    return {"schema_version": 2, "id": script.id, "name": script.name,
            "base_timeline": {"path": reference,
                              "raw_timeline_sha256": script.base_timeline.raw_timeline_sha256,
                              "structure_sha256": script.base_timeline.structure_sha256},
            "text_overrides": dict(script.text_overrides),
            "created_at": script.created_at, "updated_at": script.updated_at}


def script_from_dict(value: object, *, source_path: Path | None = None) -> ScriptVariant:
    if isinstance(value, dict) and "source_timeline_sha256" in value:
        raise ValidationError("legacy snapshot script; use script migrate --timeline <reviewed base> --output <new file>")
    data = _fields(value, {"schema_version", "id", "name", "base_timeline", "text_overrides",
                           "created_at", "updated_at"}, "script")
    if type(data["schema_version"]) is not int or data["schema_version"] != 2:
        raise ValidationError("unsupported script schema_version; expected 2")
    ref = _fields(data["base_timeline"], {"path", "raw_timeline_sha256", "structure_sha256"}, "base_timeline")
    if not isinstance(ref["path"], str) or not ref["path"].strip():
        raise ValidationError("base path must be a non-empty string")
    base_path = Path(ref["path"])
    if not base_path.is_absolute():
        if source_path is None:
            raise ValidationError("relative base reference requires script file location")
        base_path = source_path.resolve().parent / base_path
    return ScriptVariant(data["id"], data["name"],
                         BaseTimelineReference(base_path, ref["raw_timeline_sha256"], ref["structure_sha256"]),
                         data["text_overrides"], data["created_at"], data["updated_at"])


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read script JSON at {path}: {exc}") from exc


def load_script(path: Path) -> ScriptVariant:
    return script_from_dict(_read_json(path), source_path=path)


def migrate_script(path: Path, base, *, base_path: Path) -> ScriptVariant:
    data = _fields(_read_json(path), {"id", "name", "source_timeline_sha256", "segments"}, "legacy script")
    if data["source_timeline_sha256"] != timeline_sha256(base.to_timeline()):
        raise ValidationError("cannot confirm legacy base fingerprint; recreate this variant from reviewed subtitles")
    if not isinstance(data["segments"], list):
        raise ValidationError("legacy segments must be an array")
    segments = tuple(ScriptSegment(**_fields(s, {"id", "text"}, "legacy segment")) for s in data["segments"])
    if tuple(s.id for s in segments) != tuple(s.id for s in base.segments):
        raise ValidationError("legacy segment IDs, count and order must match the reviewed base")
    variant = create_script(base, base_path=base_path, id=data["id"], name=data["name"])
    overrides = {s.id: s.text for s, original in zip(segments, base.segments) if s.text != original.text}
    return replace(variant, text_overrides=overrides)


def save_script(script: ScriptVariant, path: Path, *, overwrite: bool = False) -> None:
    data = json.dumps(script_to_dict(script, destination=path), ensure_ascii=False, indent=2) + "\n"
    if not overwrite and path.exists():
        raise ValidationError(f"script output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".script-", suffix=".partial", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # Atomic publication with exclusive destination creation on NTFS and POSIX.
            os.link(temporary, path)
    except FileExistsError as exc:
        raise ValidationError(f"script output already exists: {path}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_script_selections(path: Path) -> tuple[ScriptDubbingSelection, ...]:
    data = _read_json(path)
    if not isinstance(data, list):
        raise ValidationError("variants file must contain a JSON array")
    selections = []
    for index, item in enumerate(data):
        fields = _fields(item, {"script", "voices"}, f"script selection {index}")
        if not isinstance(fields["script"], str) or not fields["script"].strip():
            raise ValidationError("script file path must be a non-empty string")
        script_path = Path(fields["script"])
        if not script_path.is_absolute():
            script_path = path.resolve().parent / script_path
        if not isinstance(fields["voices"], list):
            raise ValidationError("voices must be a JSON array")
        voices = tuple(VoiceProfile(**_fields(voice, {"id", "name", "provider", "voice_id", "model_id"},
                                             f"voice in script selection {index}"))
                       for voice in fields["voices"])
        selections.append(ScriptDubbingSelection(load_script(script_path), voices))
    return tuple(selections)
