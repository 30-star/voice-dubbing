"""CLI wiring for an ASR review pause or a prepared corrected multi-voice job."""

import json
from pathlib import Path
from uuid import uuid4

from .asr.factory import create_asr, asr_cache_directory
from .batch_pipeline import dub_video, run_batch
from .correction import resolve_tts_timeline
from .errors import MediaError
from .models import AwaitingCorrection, BatchDubbingJob
from .tts import CachedTTSProvider, default_registry


def run_dub_video(args, profiles, options: dict) -> int:
    root = Path(__file__).resolve().parents[1]
    output = args.output_dir or root / "output" / f"{args.video.stem[:40]}-batch-{uuid4().hex[:8]}"
    if not args.video.is_file():
        raise MediaError(f"source video not found: {args.video}")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise MediaError(f"output directory must be empty: {output}")
    source = None
    if args.timeline is not None:
        timeline, source = resolve_tts_timeline(args.timeline)
    # Pure local ASR/review requires no ElevenLabs key or initialized TTS adapter.
    registry = default_registry()
    configs = ({voice.id: registry.configuration(voice.provider, model_id=voice.model_id,
                 options=options, settings=options.get("provider_settings")) for voice in profiles}
                if args.timeline is not None or args.accept_asr else {})
    for config in configs.values():
        registry.create(config)  # Validate every adapter before any paid request.
    cache_dir = args.cache_dir or root / "cache" / "tts"

    def factory(voice):
        provider = registry.create(configs[voice.id])
        provider.selected_voice_name = voice.name
        return CachedTTSProvider(provider, cache_dir, enabled=not args.no_cache)

    if args.timeline is not None:
        result = run_batch(BatchDubbingJob(args.video.resolve(), timeline, profiles), output_dir=output,
            provider_factory=factory, language=args.language, ffmpeg_path=args.ffmpeg,
            ffprobe_path=args.ffprobe, subtitle_source=source, asr_calls=0,
            tts_concurrency=args.tts_concurrency)
    else:
        asr = create_asr(args, output, model=args.asr_model)
        result = dub_video(args.video, voices=profiles, asr_provider=asr, provider_factory=factory,
            output_dir=output, language=args.language, ffmpeg_path=args.ffmpeg,
            ffprobe_path=args.ffprobe, accept_asr=args.accept_asr, asr_cache_dir=asr_cache_directory(args),
            tts_concurrency=args.tts_concurrency)
    if isinstance(result, AwaitingCorrection):
        print(json.dumps({"status": result.status, "subtitle_dir": str(result.subtitle_dir),
                          "raw_timeline": str(result.subtitle_dir / "raw_timeline.json"),
                          "corrected_timeline": str(result.subtitle_dir / "corrected_timeline.json"),
                          "reviewed": False,
                          "asr_calls": json.loads((output / "asr_report.json").read_text(encoding="utf-8"))["asr_calls"]}, ensure_ascii=False))
        return 0
    print(json.dumps({"status": result.status, "report": str(result.report_path),
        "timeline": str(result.report_path.parent / "timeline.json"), "asr_calls": result.asr_calls,
        "voices": [{"id": item.voice.id, "status": item.status,
                    "audio": str(item.audio_path) if item.audio_path else None,
                    "video": str(item.video_path) if item.video_path else None,
                    "error": item.error} for item in result.voices]}, ensure_ascii=False))
    return 0 if result.status == "succeeded" else 3 if result.status == "partial_success" else 2
