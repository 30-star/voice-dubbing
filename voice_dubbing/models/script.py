"""Sparse text variants bound to reviewed subtitles by ASR identity and timing."""

import hashlib
import json
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..errors import ValidationError
from .batch import VoiceDubbingResult, VoiceProfile, validate_voice_profiles
from .transcript import TranscriptTimeline, _nonempty
from .correction import CorrectedTimeline, _sha256


@dataclass(frozen=True, slots=True)
class ScriptSegment:
    id: str
    text: str

    def __post_init__(self) -> None:
        _nonempty(self.id, "script segment id")
        _nonempty(self.text, "script segment text")


@dataclass(frozen=True, slots=True)
class BaseTimelineReference:
    path: Path
    raw_timeline_sha256: str
    structure_sha256: str

    def __post_init__(self):
        if not isinstance(self.path, Path):
            raise ValidationError("base path must be a Path")
        object.__setattr__(self, "path", self.path.resolve())
        _sha256(self.raw_timeline_sha256, "raw_timeline_sha256")
        _sha256(self.structure_sha256, "structure_sha256")


def timeline_structure_sha256(timeline: TranscriptTimeline) -> str:
    data = {"duration_ms": timeline.duration_ms, "segments": [
        {"id": s.id, "start_ms": s.start_ms, "end_ms": s.end_ms} for s in timeline.segments]}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ScriptVariant:
    id: str
    name: str
    base_timeline: BaseTimelineReference
    text_overrides: Mapping[str, str]
    created_at: str
    updated_at: str

    def __post_init__(self):
        _nonempty(self.id, "script id")
        _nonempty(self.name, "script name")
        if not isinstance(self.base_timeline, BaseTimelineReference):
            raise ValidationError("base_timeline must be a BaseTimelineReference")
        if not isinstance(self.text_overrides, Mapping):
            raise ValidationError("text_overrides must be an object")
        for id, text in self.text_overrides.items():
            _nonempty(id, "override segment id")
            _nonempty(text, "override text")
        object.__setattr__(self, "text_overrides", MappingProxyType(dict(self.text_overrides)))
        dates = []
        for value in (self.created_at, self.updated_at):
            try:
                date = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if date.utcoffset() != timezone.utc.utcoffset(date):
                    raise ValueError("not UTC")
                dates.append(date)
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValidationError("script timestamps must be UTC ISO 8601") from exc
        if dates[1] < dates[0]:
            raise ValidationError("updated_at must not precede created_at")


def validate_script_binding(script: ScriptVariant, base: CorrectedTimeline) -> None:
    if not isinstance(script, ScriptVariant) or not isinstance(base, CorrectedTimeline):
        raise ValidationError("script requires a CorrectedTimeline base")
    if not base.reviewed:
        raise ValidationError("script base must be reviewed")
    if script.base_timeline.raw_timeline_sha256 != base.raw_timeline_sha256:
        raise ValidationError("script raw ASR fingerprint does not match the base")
    if script.base_timeline.structure_sha256 != timeline_structure_sha256(base.to_timeline()):
        raise ValidationError("script time structure fingerprint does not match the base")
    if set(script.text_overrides) - {s.id for s in base.segments}:
        raise ValidationError("unknown script segment id in overrides")


def script_sha256(script: ScriptVariant) -> str:
    data = {"id": script.id, "name": script.name, "created_at": script.created_at,
            "updated_at": script.updated_at, "text_overrides": dict(script.text_overrides),
            "raw_timeline_sha256": script.base_timeline.raw_timeline_sha256,
            "structure_sha256": script.base_timeline.structure_sha256}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ScriptDubbingSelection:
    script: ScriptVariant
    voices: tuple[VoiceProfile, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.script, ScriptVariant):
            raise ValidationError("script must be a ScriptVariant")
        object.__setattr__(self, "voices", validate_voice_profiles(self.voices))


@dataclass(frozen=True, slots=True)
class ScriptBatchDubbingJob:
    source_video: Path
    base_timeline: CorrectedTimeline
    selections: tuple[ScriptDubbingSelection, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source_video, Path):
            raise ValidationError("source_video must be a Path")
        if not isinstance(self.base_timeline, CorrectedTimeline):
            raise ValidationError("base_timeline must be a CorrectedTimeline")
        try:
            selections = tuple(self.selections)
        except TypeError as exc:
            raise ValidationError("selections must be a sequence") from exc
        if not selections or any(not isinstance(item, ScriptDubbingSelection) for item in selections):
            raise ValidationError("selections must contain at least one ScriptDubbingSelection")
        if len({item.script.id.casefold() for item in selections}) != len(selections):
            raise ValidationError("duplicate script variant id")
        for item in selections:
            validate_script_binding(item.script, self.base_timeline)
        object.__setattr__(self, "selections", selections)


    @property
    def timeline(self) -> TranscriptTimeline:
        return self.base_timeline.to_timeline()


@dataclass(frozen=True, slots=True)
class ScriptVoiceResult:
    script: ScriptVariant
    result: VoiceDubbingResult


@dataclass(frozen=True, slots=True)
class ScriptBatchDubbingResult:
    job: ScriptBatchDubbingJob
    status: str
    combinations: tuple[ScriptVoiceResult, ...]
    report_path: Path
