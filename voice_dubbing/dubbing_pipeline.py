"""Provider-neutral single-voice Timeline-to-WAV orchestration."""

import csv
import json
import math
from pathlib import Path

from .audio.dubbing import SAMPLES_PER_MS, mix_speech, normalize_speech
from .errors import MediaError, ProviderError, ValidationError
from .duration import DURATION_TOLERANCE_MS, MatchedSpeech, match_speech
from .models import AudioDubbingResult, DubbingSegment, SpeechRequest, TranscriptTimeline
from .tts import TTSProvider
from .tts.concurrent import ordered_synthesis, validate_concurrency


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
    match_duration: bool = True,
    tts_concurrency: int = 1,
) -> None:
    order = {segment.id: index for index, segment in enumerate(segments)}
    synthesis_events = sorted(synthesis_events or [], key=lambda event: order.get(event["segment_id"], len(order)))
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
        "playback_audio_path": str(segment.playback_audio_path) if segment.playback_audio_path else None,
        "playback_duration_ms": segment.playback_duration_ms,
        "speed_factor": segment.speed_factor,
        "duration_adjusted": segment.speed_factor > 1,
        "playback_overlap_with_next_ms": segment.playback_overlap_with_next_ms,
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
        "duration_matching": match_duration,
        "tts_concurrency": tts_concurrency,
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
            "playback_audio_path", "playback_duration_ms", "speed_factor",
            "duration_adjusted", "playback_overlap_with_next_ms",
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
    match_duration: bool = True,
    tts_concurrency: int = 1,
) -> AudioDubbingResult:
    validate_concurrency(tts_concurrency)
    if not isinstance(timeline, TranscriptTimeline):
        raise ValidationError("timeline must be a TranscriptTimeline")
    if not isinstance(voice_id, str) or not voice_id.strip():
        raise ValidationError("voice_id must be a non-empty string")
    if not timeline.segments and timeline.duration_ms == 0:
        raise ValidationError("a zero-length empty timeline cannot produce a WAV")
    if match_duration and any(a.start_ms == b.start_ms for a, b in zip(timeline.segments, timeline.segments[1:])):
        raise ValidationError("simultaneous subtitle starts cannot be aligned without moving timestamps")
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
    synthesis_failures = []
    def generate(item):
        index, segment = item
        sentence_dir = segments_dir / f"{index + 1:06d}"
        sentence_dir.mkdir()
        request = SpeechRequest(segment.id, segment.text, voice_id, language)
        try:
            return index, segment, sentence_dir, provider.synthesize(request, output_dir=sentence_dir)
        except Exception:
            synthesis_failures.append(segment.id)
            raise

    speech_results = ordered_synthesis(enumerate(timeline.segments), generate, tts_concurrency)
    try:
        for index, segment, sentence_dir, speech in speech_results:
            failed_segment_id = segment.id
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
            window_end = min(segment.end_ms, next_start) if next_start is not None else segment.end_ms
            playback = (match_speech(normalized, sentence_dir / "matched.wav",
                                    window_end - segment.start_ms,
                                    logs_dir / f"match-{index + 1:06d}.log",
                                    ffmpeg_path=ffmpeg_path)
                        if match_duration else MatchedSpeech(normalized, frames))
            playback_ms = math.ceil(playback.frames / SAMPLES_PER_MS)
            playback_overlap_ms = (max(0, segment.start_ms + playback_ms - next_start)
                                   if next_start is not None else 0)
            segment_warnings = []
            if overrun_ms > DURATION_TOLERANCE_MS:
                segment_warnings.append(f"segment {segment.id} exceeds subtitle end by {overrun_ms} ms")
            if overlap_ms > DURATION_TOLERANCE_MS:
                segment_warnings.append(f"segment {segment.id} original speech overlaps next start by {overlap_ms} ms")
            if playback.speed_factor > 1:
                segment_warnings.append(f"segment {segment.id} fitted at {playback.speed_factor:.3f}x; "
                                        f"{duration_ms} ms -> {playback_ms} ms")
            if playback.speed_factor > 1.5:
                segment_warnings.append(f"segment {segment.id} requires noticeable acceleration; check listening quality")
            warnings.extend(segment_warnings)
            completed.append(DubbingSegment(
                segment.id, segment.start_ms, segment.end_ms, segment.text, source,
                duration_ms, target_ms, overrun_ms, overlap_ms, tuple(segment_warnings),
                playback.path, playback_ms, playback.speed_factor, playback_overlap_ms,
            ))
            clips.append((playback.path, segment.start_ms))
            end_frame = max(end_frame, segment.start_ms * SAMPLES_PER_MS + playback.frames)
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
            match_duration=match_duration,
            tts_concurrency=tts_concurrency,
        )
        return AudioDubbingResult(
            provider_id, voice_id, is_mock, timeline.duration_ms, output_ms,
            final_audio, tuple(completed), tuple(warnings),
        )
    except Exception as exc:
        speech_results.close()
        # A failed prefetch may be ahead of the last consumed sentence.
        failed_events = [e for e in getattr(provider, "cache_events", [])[previous_events:]
                         if e.get("status") == "failed"]
        if failed_events:
            failed_segment_id = failed_events[0]["segment_id"]
        elif synthesis_failures:
            failed_segment_id = synthesis_failures[0]
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
            match_duration=match_duration,
            tts_concurrency=tts_concurrency,
        )
        raise
