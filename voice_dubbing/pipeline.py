"""Standalone video-to-timeline operation, independent of the WPF application."""

from pathlib import Path
import json

from .asr import ASRProvider, VideoASRProvider
from .asr.cache import cache_key, restore_cache, publish_cache
from .audio.extraction import extract_audio, probe_video_duration_ms
from .errors import MediaError
from .models import TranscriptTimeline
from .correction import initialize_correction


def transcribe_video(
    video_path: Path,
    *,
    provider: ASRProvider,
    output_dir: Path,
    language: str | None = None,
    ffmpeg_path: Path | None = None,
    ffprobe_path: Path | None = None,
    cache_dir: Path | None = None,
) -> TranscriptTimeline:
    if not video_path.is_file():
        raise MediaError(f"video file not found: {video_path}")
    video_path = video_path.resolve()
    output_dir = output_dir.resolve()
    if output_dir == video_path.parent:
        raise MediaError("output directory must differ from the source video directory")
    timeline_path = output_dir / "timeline.json"
    if any((output_dir / name).exists() for name in ("timeline.json", "raw_timeline.json", "corrected_timeline.json")):
        raise MediaError(f"timeline already exists; choose a fresh output directory: {timeline_path}")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise MediaError(f"ASR output directory must be empty: {output_dir}")
    duration_ms = probe_video_duration_ms(video_path, ffprobe_path=ffprobe_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    key = cache_key(video_path, provider, language) if cache_dir else None
    recognized = restore_cache(cache_dir, key, output_dir) if cache_dir else None
    hit = recognized is not None
    extracted = False
    if not hit:
        if isinstance(provider, VideoASRProvider):
            recognized = provider.transcribe_video(video_path, language=language)
        else:
            audio_path = output_dir / "source_audio.wav"
            extract_audio(video_path, audio_path, ffmpeg_path=ffmpeg_path)
            extracted = True
            recognized = provider.transcribe(audio_path, language=language)
    timeline = TranscriptTimeline(recognized.segments, max(duration_ms, recognized.duration_ms))
    initialize_correction(timeline, output_dir)
    report = {"provider": provider.provider_id, "parameters": getattr(provider, "cache_identity", {}),
        "cache_key": key, "cache_hit": hit, "asr_calls": 0 if hit else 1,
        "audio_extraction_calls": int(extracted), "language": language or "auto",
        "source_video": str(video_path), "duration_ms": timeline.duration_ms}
    warning = publish_cache(cache_dir, key, timeline, output_dir, provenance=report) if cache_dir and not hit else None
    report["warnings"] = [warning] if warning else []
    (output_dir / "asr_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return timeline
