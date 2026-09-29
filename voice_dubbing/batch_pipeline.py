"""One video transcription followed by independent, serial voice renders."""

import hashlib
import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .asr import ASRProvider
from .dubbing_pipeline import synthesize_timeline
from .errors import MediaError, ValidationError
from .models import (BatchDubbingJob, BatchDubbingResult, VoiceDubbingResult,
                     VoiceProfile, TranscriptTimeline, AwaitingCorrection)
from .correction import resolve_tts_timeline, save_correction
from .models.batch import validate_voice_profiles
from .pipeline import transcribe_video
from .renderer import RenderedVideo, render_dubbed_video
from .timeline import load_timeline, save_timeline
from .models.transcript import timeline_sha256
from .tts import TTSProvider
from .tts.concurrent import IsolatedTTSProvider, validate_concurrency


ProviderFactory = Callable[[VoiceProfile], TTSProvider]
VideoRenderer = Callable[..., RenderedVideo]


def _safe_id(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]", "_", value).strip("._-")[:48] or "voice"
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if clean != value or clean.upper() in reserved:
        clean += "-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return clean


def _timeline_hash(timeline: TranscriptTimeline) -> str:
    return timeline_sha256(timeline)


def _redact(value: str) -> str:
    secret = os.environ.get("ELEVENLABS_API_KEY")
    return value.replace(secret, "[REDACTED]") if secret else value


def _result_dict(result: VoiceDubbingResult) -> dict:
    return {
        "voice": asdict(result.voice), "status": result.status,
        "audio_path": str(result.audio_path) if result.audio_path else None,
        "video_path": str(result.video_path) if result.video_path else None,
        "output_duration_ms": result.output_duration_ms,
        "video_duration_ms": result.video_duration_ms,
        "warnings": list(result.warnings), "error": result.error,
        "cache_hits": result.cache_hits, "supplier_requests": result.supplier_requests,
        "generated_segments": result.generated_segments,
        "segment_cache": list(result.segment_cache),
    }


def _write_report(job: BatchDubbingJob, output_dir: Path,
                  completed: list[VoiceDubbingResult], asr_calls: int,
                  subtitle_source: dict | None = None) -> Path:
    successes = sum(item.status == "succeeded" for item in completed)
    status = ("succeeded" if successes == len(job.voices) else
              "failed" if len(completed) == len(job.voices) and successes == 0 else
              "partial_success" if len(completed) == len(job.voices) else "running")
    report = {
        "status": status,
        "source_video": str(job.source_video.resolve()),
        "source_audio": str(output_dir / "source_audio.wav") if (output_dir / "source_audio.wav").is_file() else None,
        "timeline_path": str(output_dir / "timeline.json"),
        "timeline_sha256": _timeline_hash(job.timeline),
        "timeline_duration_ms": job.timeline.duration_ms,
        "segments": len(job.timeline.segments),
        "asr_calls": asr_calls,
        "completed_voices": len(completed),
        "total_voices": len(job.voices),
        "cache_hits": sum(item.cache_hits for item in completed),
        "supplier_requests": sum(item.supplier_requests for item in completed),
        "generated_segments": sum(item.generated_segments for item in completed),
        "subtitle_source": subtitle_source,
        "voices": [_result_dict(item) for item in completed],
    }
    report_path = output_dir / "batch_report.json"
    partial = output_dir / "batch_report.partial.json"
    partial.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    partial.replace(report_path)
    return report_path


def render_voice(
    source_video: Path, timeline: TranscriptTimeline, voice: VoiceProfile, *,
    output_dir: Path, provider_factory: ProviderFactory, video_name: str | None = None,
    language: str | None = None, ffmpeg_path: Path | None = None,
    ffprobe_path: Path | None = None, renderer: VideoRenderer = render_dubbed_video,
    subtitle_source: dict | None = None,
    script_context: dict | None = None,
    tts_concurrency: int = 1,
) -> VoiceDubbingResult:
    """One independent combination, shared by voice and script batches."""
    provider: TTSProvider | None = None
    audio_path: Path | None = None
    output_duration_ms: int | None = None
    warnings: list[str] = []
    previous_hits = previous_requests = previous_warnings = previous_events = previous_calls = 0
    video_name = video_name or f"{source_video.stem}_{_safe_id(voice.id)}.mp4"
    rendered: RenderedVideo | None = None
    error: str | None = None
    try:
        output_dir.mkdir()
        provider = provider_factory(voice)
        if tts_concurrency > 1:
            provider = IsolatedTTSProvider(provider, lambda: provider_factory(voice))
        previous_hits = getattr(provider, "cache_hits", 0)
        previous_requests = getattr(provider, "supplier_requests", 0)
        previous_warnings = len(getattr(provider, "warnings", []))
        previous_events = len(getattr(provider, "cache_events", []))
        previous_calls = getattr(provider, "provider_calls", 0)
        if provider.provider_id != voice.provider:
            raise ValidationError(f"voice {voice.id} provider mismatch")
        audio = synthesize_timeline(timeline, provider=provider, voice_id=voice.voice_id,
                                    output_dir=output_dir, language=language, ffmpeg_path=ffmpeg_path,
                                    subtitle_source=subtitle_source, script_context=script_context,
                                    tts_concurrency=tts_concurrency)
        audio_path = audio.audio_path
        output_duration_ms = audio.output_duration_ms
        warnings.extend(audio.warnings)
        rendered = renderer(source_video, audio.audio_path, output_dir / video_name,
                            ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path)
    except Exception as exc:
        error = _redact(str(exc) or type(exc).__name__)
    warnings.extend(getattr(provider, "warnings", [])[previous_warnings:] if provider else [])
    order = {segment.id: index for index, segment in enumerate(timeline.segments)}
    events = tuple(sorted(getattr(provider, "cache_events", [])[previous_events:],
                          key=lambda event: order.get(event["segment_id"], len(order)))) if provider else ()
    generated = sum(bool(event["generated"]) for event in events) if events else (
        getattr(provider, "provider_calls", 0) - previous_calls if provider else 0)
    return VoiceDubbingResult(
        voice, "succeeded" if rendered is not None else "failed", audio_path,
        rendered.path if rendered else None, tuple(warnings), error,
        output_duration_ms, rendered.duration_ms if rendered else None,
        getattr(provider, "cache_hits", 0) - previous_hits if provider else 0,
        getattr(provider, "supplier_requests", 0) - previous_requests if provider else 0,
        generated, events,
    )


def run_batch(
    job: BatchDubbingJob, *, output_dir: Path, provider_factory: ProviderFactory,
    language: str | None = None, ffmpeg_path: Path | None = None,
    ffprobe_path: Path | None = None, renderer: VideoRenderer = render_dubbed_video,
    asr_calls: int = 0,
    subtitle_source: dict | None = None,
    tts_concurrency: int = 1,
) -> BatchDubbingResult:
    """Render a prepared Timeline; useful for cache reuse without another ASR run."""
    validate_concurrency(tts_concurrency)
    if not isinstance(job, BatchDubbingJob):
        raise ValidationError("job must be a BatchDubbingJob")
    if not job.source_video.is_file():
        raise MediaError(f"source video not found: {job.source_video}")
    output_dir = output_dir.resolve()
    if output_dir == job.source_video.resolve().parent:
        raise MediaError("output directory must differ from the source video directory")
    if output_dir.exists():
        extras = {item.name for item in output_dir.iterdir()} - {
            "source_audio.wav", "timeline.json", "raw_timeline.json", "corrected_timeline.json",
            "subtitle.srt", "asr_logs", "asr_tmp", "asr_report.json"}
        if extras:
            raise MediaError(f"output directory must be empty except for ASR artifacts: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    timeline_path = output_dir / "timeline.json"
    if timeline_path.exists():
        if load_timeline(timeline_path) != job.timeline:
            raise ValidationError("existing timeline.json does not match the batch job")
    else:
        save_timeline(job.timeline, timeline_path)
    completed: list[VoiceDubbingResult] = []
    report_path = _write_report(job, output_dir, completed, asr_calls, subtitle_source)
    for voice in job.voices:
        completed.append(render_voice(
            job.source_video, job.timeline, voice, output_dir=output_dir / _safe_id(voice.id),
            provider_factory=provider_factory, language=language, ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path, renderer=renderer,
            subtitle_source=subtitle_source,
            tts_concurrency=tts_concurrency,
        ))
        report_path = _write_report(job, output_dir, completed, asr_calls, subtitle_source)
    successes = sum(item.status == "succeeded" for item in completed)
    status = ("succeeded" if successes == len(completed) else
              "partial_success" if successes else "failed")
    return BatchDubbingResult(job, status, tuple(completed), report_path, asr_calls)


def dub_video(
    video_path: Path, *, voices: tuple[VoiceProfile, ...], asr_provider: ASRProvider,
    provider_factory: ProviderFactory, output_dir: Path, language: str | None = None,
    ffmpeg_path: Path | None = None, ffprobe_path: Path | None = None,
    renderer: VideoRenderer = render_dubbed_video,
    accept_asr: bool = False,
    asr_cache_dir: Path | None = None,
    tts_concurrency: int = 1,
) -> BatchDubbingResult | AwaitingCorrection:
    """Recognize once and stop for review unless ASR text is explicitly accepted."""
    validate_concurrency(tts_concurrency)
    voices = validate_voice_profiles(voices)
    if not video_path.is_file():
        raise MediaError(f"source video not found: {video_path}")
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise MediaError(f"output directory must be empty: {output_dir}")
    timeline = transcribe_video(video_path, provider=asr_provider, output_dir=output_dir,
                                language=language, ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path,
                                cache_dir=asr_cache_dir)
    if not accept_asr:
        return AwaitingCorrection(timeline, output_dir,
            asr_calls=json.loads((output_dir / "asr_report.json").read_text(encoding="utf-8"))["asr_calls"])
    save_correction(output_dir)
    timeline, subtitle_source = resolve_tts_timeline(output_dir)
    job = BatchDubbingJob(video_path.resolve(), timeline, voices)
    return run_batch(job, output_dir=output_dir, provider_factory=provider_factory,
                     language=language, ffmpeg_path=ffmpeg_path,
                     ffprobe_path=ffprobe_path, renderer=renderer,
                     asr_calls=json.loads((output_dir / "asr_report.json").read_text(encoding="utf-8"))["asr_calls"],
                     subtitle_source=subtitle_source, tts_concurrency=tts_concurrency)
