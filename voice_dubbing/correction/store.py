"""ASR preservation, draft lifecycle and reviewed input resolution."""

from dataclasses import replace
from pathlib import Path

from ..errors import ValidationError
from ..models.correction import CorrectedTimeline, CorrectionDraft
from ..models.transcript import TranscriptTimeline, timeline_sha256
from ..timeline import load_timeline
from ..timeline.serialization import timeline_to_dict
from .operations import create_correction, restore_all, restore_segment, set_text
from .serialization import (corrected_sha256, corrected_to_dict, load_corrected, load_draft,
                            save_draft, write_json_atomic)


RAW = "raw_timeline.json"
CORRECTED = "corrected_timeline.json"
DRAFT = "correction_draft.json"


def initialize_correction(raw: TranscriptTimeline, folder: Path) -> CorrectedTimeline:
    targets = [folder / name for name in (RAW, CORRECTED, "timeline.json", DRAFT)]
    if any(path.exists() for path in targets):
        raise ValidationError(f"timeline already exists; choose a fresh correction directory: {folder}")
    corrected = create_correction(raw)
    folder.mkdir(parents=True, exist_ok=True)
    write_json_atomic(timeline_to_dict(raw), folder / RAW)
    write_json_atomic(timeline_to_dict(raw), folder / "timeline.json")
    write_json_atomic(corrected_to_dict(corrected), folder / CORRECTED)
    return corrected


def load_saved(folder: Path) -> CorrectedTimeline:
    corrected = load_corrected(folder / CORRECTED)
    corrected.validate_raw(load_timeline(folder / RAW))
    return corrected


def _working(folder: Path, saved: CorrectedTimeline) -> CorrectionDraft:
    path = folder / DRAFT
    if not path.exists():
        return CorrectionDraft(corrected_sha256(saved), saved)
    draft = load_draft(path)
    draft.timeline.validate_raw(load_timeline(folder / RAW))
    if draft.base_corrected_sha256 != corrected_sha256(saved):
        # Recover a completed save if only subsequent draft cleanup failed.
        if replace(draft.timeline, reviewed=True) == saved:
            return CorrectionDraft(corrected_sha256(saved), saved)
        raise ValidationError("stale correction draft; saved subtitles changed; inspect or discard the draft")
    return draft


def edit_correction(folder: Path, *, segment_id: str | None = None, text: str | None = None,
                    restore: bool = False, all_segments: bool = False) -> None:
    saved = load_saved(folder)
    draft = _working(folder, saved)
    if all_segments:
        updated = restore_all(draft.timeline)
    elif restore:
        updated = restore_segment(draft.timeline, segment_id)
    else:
        updated = set_text(draft.timeline, segment_id, text)
    updated.validate_raw(load_timeline(folder / RAW))
    save_draft(replace(draft, timeline=updated), folder / DRAFT)


def save_correction(folder: Path) -> CorrectedTimeline:
    saved = load_saved(folder)
    draft = _working(folder, saved)
    corrected = replace(draft.timeline, reviewed=True)
    corrected.validate_raw(load_timeline(folder / RAW))
    # Check again immediately before publication; never silently apply an obsolete draft.
    if corrected_sha256(load_saved(folder)) != draft.base_corrected_sha256:
        raise ValidationError("stale correction draft; saved subtitles changed")
    write_json_atomic(corrected_to_dict(corrected), folder / CORRECTED, overwrite=True)
    try:
        (folder / DRAFT).unlink(missing_ok=True)
    except OSError:
        # The saved file is the commit point. A completed draft is recognized by _working.
        pass
    return corrected


def discard_correction(folder: Path) -> None:
    load_saved(folder)
    (folder / DRAFT).unlink(missing_ok=True)


def inspect_correction(folder: Path) -> dict:
    saved = load_saved(folder)
    path = folder / DRAFT
    draft = load_draft(path) if path.exists() else None
    if draft:
        draft.timeline.validate_raw(load_timeline(folder / RAW))
    working = draft.timeline if draft else saved
    rows = [{"id": original.id, "start_ms": original.start_ms, "end_ms": original.end_ms,
             "original_text": original.original_text, "text": original.text, "edited": original.edited,
             "draft_text": current.text, "draft_edited": current.edited,
             "unsaved": original.text != current.text}
            for original, current in zip(saved.segments, working.segments)]
    return {"subtitle_dir": str(folder.resolve()), "reviewed": saved.reviewed,
            "raw_timeline_sha256": saved.raw_timeline_sha256,
            "corrected_timeline_sha256": timeline_sha256(saved.to_timeline()),
            "edited_segments": sum(s.edited for s in saved.segments),
            "has_draft": draft is not None, "unsaved_segments": sum(row["unsaved"] for row in rows),
            "stale_draft": bool(draft and draft.base_corrected_sha256 != corrected_sha256(saved)
                                 and replace(draft.timeline, reviewed=True) != saved), "segments": rows}


def resolve_corrected_timeline(path: Path) -> tuple[CorrectedTimeline, dict]:
    folder = path.resolve() if path.is_dir() else path.resolve().parent
    if not path.is_dir() and path.name not in {RAW, CORRECTED, "timeline.json"}:
        raise ValidationError("TTS requires a correction directory or corrected_timeline.json; run correction init and save")
    if not (folder / CORRECTED).is_file():
        raise ValidationError("corrected_timeline.json missing; run correction init <timeline.json> --output-dir <dir>, then correction save")
    if not path.is_dir() and not path.is_file():
        raise ValidationError(f"subtitle input not found: {path}")
    corrected = load_saved(folder)
    if not corrected.reviewed:
        raise ValidationError("subtitles await human review; run correction inspect and correction save before TTS")
    timeline = corrected.to_timeline()
    source = {"kind": "corrected", "raw_timeline_path": str(folder / RAW),
              "corrected_timeline_path": str(folder / CORRECTED), "reviewed": True,
              "raw_timeline_sha256": corrected.raw_timeline_sha256,
              "corrected_timeline_sha256": timeline_sha256(timeline),
              "corrected_document_sha256": corrected_sha256(corrected),
              "segments": [{"id": s.id, "original_text": s.original_text, "text": s.text,
                            "edited": s.edited} for s in corrected.segments]}
    return corrected, source


def resolve_tts_timeline(path: Path) -> tuple[TranscriptTimeline, dict]:
    corrected, source = resolve_corrected_timeline(path)
    return corrected.to_timeline(), source


def load_script_base(path: Path) -> CorrectedTimeline:
    return resolve_corrected_timeline(path)[0]
