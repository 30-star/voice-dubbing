"""Provider-neutral single-voice Timeline-to-WAV orchestration."""

import csv
import json
import math
from pathlib import Path

from .audio.dubbing import SAMPLES_PER_MS, mix_speech, normalize_speech
from .errors import MediaError, ProviderError, ValidationError
from .models import AudioDubbingResult, DubbingSegment, SpeechRequest, TranscriptTimeline
from .tts import TTSProvider


DURATION_TOLERANCE_MS = 150


def _reports(
    output_dir: Path, *, status: str, provider_id: str, voice_id: str, is_mock: bool,
    timeline_duration_ms: int, output_duration_ms: int | None,
    audio_path: Path | None, segments: list[DubbingSegment], warnings: list[str],
    error: str | None = None, failed_segment_id: str | None = None,
    voice_name: str | None = None, model_id: str | None = None,
    output_format: str | None = None,
    synthesis_events: list[dict] | None = None,
    subtitle_source: dict | None = None,
    script_context: dict | None = None,
) -> None:
    events_by_id = {event["segment_id"]: event for event in (synthesis_events or [])}
    source_by_id = {item["id"]: item for item in (subtitle_source or {}).get("segments", [])}
    script_rows = {item["id"]: item for item in (script_context or {}).get("segments", [])}
    rows = [{
        "id": segment.id,
        "start_ms": segment.start_ms,
        "end_ms": segment.end_ms,
        "text": segment.text,
        "audio_path": str(segment.audio_path),
        "audio_duration_ms": segment.audio_duration_ms,
        "actual_duration_ms": segment.actual_duration_ms,
        "target_duration_ms": segment.target_duration_ms,
        "overrun_ms": segment.overrun_ms,
        "overflow_ms": segment.overflow_ms,
        "voice_id": voice_id,
        "voice_name": voice_name,
        "voice_profile_id": (script_context or {}).get("voice_profile_id"),
        "variant_id": (script_context or {}).get("id"),
        "variant_name": (script_context or {}).get("name"),
        "overlap_with_next_ms": segment.overlap_with_next_ms,
        "warnings": list(segment.warnings),
        "cache_hit": events_by_id.get(segment.id, {}).get("cache_hit"),
        "generated": events_by_id.get(segment.id, {}).get("generated"),
        "supplier_requests": events_by_id.get(segment.id, {}).get("supplier_requests"),
        "subtitle_edited": source_by_id.get(segment.id, {}).get("edited"),
        "text_source": script_rows.get(segment.id, {}).get("text_source"),
    } for segment in segments]
    report = {
        "status": status,
        "provider_id": provider_id,
        "voice_id": voice_id,
        "voice_name": voice_name,
        "model_id": model_id,
        "output_format": output_format,
        "is_mock": is_mock,
        "timeline_duration_ms": timeline_duration_ms,
        "output_duration_ms": output_duration_ms,
        "duration_tolerance_ms": DURATION_TOLERANCE_MS,
        "audio_path": str(audio_path) if audio_path is not None else None,
        "segments": rows,
        "warnings": warnings,
        "error": error,
        "failed_segment_id": failed_segment_id,
        "synthesis_events": synthesis_events or [],
        "subtitle_source": subtitle_source,
        "script_variant": script_context,
    }
    (output_dir / "dubbing_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (output_dir / "dubbing_report.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "id", "start_ms", "end_ms", "text", "audio_path", "target_duration_ms",
            "audio_duration_ms", "overrun_ms", "overlap_with_next_ms", "warnings",
            "actual_duration_ms", "overflow_ms", "voice_id",
            "voice_name", "voice_profile_id", "variant_id", "variant_name",
            "cache_hit", "generated", "supplier_requests",
            "subtitle_edited", "text_source",
        ])
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "warnings": " | ".join(row["warnings"])})


def synthesize_timeline(
    timeline: TranscriptTimeline,
    *,
    provider: TTSProvider,
    voice_id: str,
    output_dir: Path,
    language: str | None = None,
    ffmpeg_path: Path | None = None,
    subtitle_source: dict | None = None,
    script_context: dict | None = None,
) -> AudioDubbingResult:
    if not isinstance(timeline, TranscriptTimeline):
        raise ValidationError("timeline must be a TranscriptTimeline")
    if not isinstance(voice_id, str) or not voice_id.strip():
        raise ValidationError("voice_id must be a non-empty string")
    if not timeline.segments and timeline.duration_ms == 0:
        raise ValidationError("a zero-length empty timeline cannot produce a WAV")
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise MediaError(f"output directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    segments_dir = output_dir / "segments"
    logs_dir = output_dir / "logs"
    segments_dir.mkdir()
    logs_dir.mkdir()
    provider_id = provider.provider_id
    previous_events = len(getattr(provider, "cache_events", []))
    is_mock = bool(getattr(provider, "is_mock", False))
    completed: list[DubbingSegment] = []
    warnings: list[str] = []
    clips: list[tuple[Path, int]] = []
    end_frame = timeline.duration_ms * SAMPLES_PER_MS
    failed_segment_id: str | None = None
    final_audio = output_dir / "dubbed_audio.wav"
    partial_audio = output_dir / "dubbed_audio.partial.wav"
    try:
        for index, segment in enumerate(timeline.segments):
            failed_segment_id = segment.id
            sentence_dir = segments_dir / f"{index + 1:06d}"
            sentence_dir.mkdir()
            request = SpeechRequest(segment.id, segment.text, voice_id, language)
            speech = provider.synthesize(request, output_dir=sentence_dir)
            if speech.segment_id != segment.id:
                raise ProviderError(f"TTS provider returned the wrong segment id for {segment.id}")
            source = speech.audio_path.resolve()
            if source.parent != sentence_dir.resolve():
                raise ProviderError(f"TTS provider wrote outside its segment directory: {source}")
            normalized = sentence_dir / "normalized.wav"
            frames = normalize_speech(
                source, normalized, logs_dir / f"normalize-{index + 1:06d}.log",
                ffmpeg_path=ffmpeg_path,
            )
            duration_ms = math.ceil(frames / SAMPLES_PER_MS)
            target_ms = segment.end_ms - segment.start_ms
            overrun_ms = max(0, duration_ms - target_ms)
            next_start = (timeline.segments[index + 1].start_ms
                          if index + 1 < len(timeline.segments) else None)
            overlap_ms = max(0, segment.start_ms + duration_ms - next_start) if next_start is not None else 0
            segment_warnings = []
            if overrun_ms > DURATION_TOLERANCE_MS:
                segment_warnings.append(f"segment {segment.id} exceeds subtitle end by {overrun_ms} ms")
            if overlap_ms > DURATION_TOLERANCE_MS:
                segment_warnings.append(f"segment {segment.id} overlaps next start by {overlap_ms} ms")
            warnings.extend(segment_warnings)
            completed.append(DubbingSegment(
                segment.id, segment.start_ms, segment.end_ms, segment.text, source,
                duration_ms, target_ms, overrun_ms, overlap_ms, tuple(segment_warnings),
            ))
            clips.append((normalized, segment.start_ms))
            end_frame = max(end_frame, segment.start_ms * SAMPLES_PER_MS + frames)
        failed_segment_id = None
        output_ms = math.ceil(end_frame / SAMPLES_PER_MS)
        if output_ms - timeline.duration_ms > DURATION_TOLERANCE_MS:
            warnings.append(f"full audio extends timeline by {output_ms - timeline.duration_ms} ms")
        mix_speech(clips, partial_audio, end_frame, logs_dir / "mix.log", ffmpeg_path=ffmpeg_path)
        partial_audio.replace(final_audio)
        _reports(
            output_dir, status="succeeded", provider_id=provider_id, voice_id=voice_id,
            is_mock=is_mock, timeline_duration_ms=timeline.duration_ms,
            output_duration_ms=output_ms, audio_path=final_audio,
            segments=completed, warnings=warnings,
            voice_name=getattr(provider, "selected_voice_name", None),
            model_id=getattr(provider, "model_id", None),
            output_format=getattr(provider, "output_format", None),
            synthesis_events=getattr(provider, "cache_events", [])[previous_events:],
            subtitle_source=subtitle_source, script_context=script_context,
        )
        return AudioDubbingResult(
            provider_id, voice_id, is_mock, timeline.duration_ms, output_ms,
            final_audio, tuple(completed), tuple(warnings),
        )
    except Exception as exc:
        if final_audio.exists():
            final_audio.unlink()
        _reports(
            output_dir, status="failed", provider_id=provider_id, voice_id=voice_id,
            is_mock=is_mock, timeline_duration_ms=timeline.duration_ms,
            output_duration_ms=None, audio_path=None, segments=completed,
            warnings=warnings, error=str(exc), failed_segment_id=failed_segment_id,
            voice_name=getattr(provider, "selected_voice_name", None),
            model_id=getattr(provider, "model_id", None),
            output_format=getattr(provider, "output_format", None),
            synthesis_events=getattr(provider, "cache_events", [])[previous_events:],
            subtitle_source=subtitle_source, script_context=script_context,
        )
        raise
