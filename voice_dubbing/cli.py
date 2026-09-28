"""Standalone command-line entry point for timeline checks and video ASR."""

import argparse
import json
import os
import sys
from uuid import uuid4
from pathlib import Path
from typing import Sequence

from .asr.factory import add_asr_options, create_asr, asr_cache_directory
from .correction import resolve_tts_timeline
from .correction.cli import add_correction_commands, run_correction_command
from .video_cli import run_dub_video
from .dubbing_pipeline import synthesize_timeline
from .errors import MediaError, ProviderError, ValidationError
from .models import VoiceProfile
from .models.batch import validate_voice_profiles
from .pipeline import transcribe_video
from .script_cli import add_script_commands, run_dub_scripts, run_script_command
from .timeline import load_timeline
from .tts import CachedTTSProvider, ElevenLabsTTSProvider, FakeTTSProvider


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voice_dubbing")
    subcommands = parser.add_subparsers(dest="command", required=True)
    validate = subcommands.add_parser("validate-timeline", help="validate a timeline JSON file")
    validate.add_argument("path", type=Path, help="UTF-8 timeline JSON path")
    transcribe = subcommands.add_parser("transcribe-video", help="recognize a video and create raw/corrected subtitles for review")
    transcribe.add_argument("video", type=Path, help="source video path")
    transcribe.add_argument("--output-dir", type=Path, help="output directory (default: module output)")
    transcribe.add_argument("--model", default="base", help="Faster-Whisper model name or local path")
    transcribe.add_argument("--device", choices=["cpu", "cuda", "auto"], default="cpu")
    transcribe.add_argument("--compute-type", default="int8")
    transcribe.add_argument("--language", help="source language code; omit for auto detection")
    transcribe.add_argument("--local-model-only", action="store_true",
                            help="use an already cached/local model")
    transcribe.add_argument("--ffmpeg", type=Path, help="FFmpeg executable path")
    transcribe.add_argument("--ffprobe", type=Path, help="FFprobe executable path")
    add_asr_options(transcribe)
    dub = subcommands.add_parser("dub-timeline", help="generate a single-voice WAV from reviewed corrected subtitles")
    dub.add_argument("timeline", type=Path, help="reviewed subtitle directory or corrected_timeline.json")
    dub.add_argument("--provider", choices=["fake", "elevenlabs"], required=True,
                     help="TTS adapter; fake produces test tones, not speech")
    dub.add_argument("--voice", help="one voice ID, or auto for an available ElevenLabs voice")
    dub.add_argument("--model-id", help="ElevenLabs model ID")
    dub.add_argument("--output-format", help="ElevenLabs MP3 or PCM output format")
    dub.add_argument("--timeout", type=float, help="ElevenLabs request timeout in seconds")
    dub.add_argument("--max-retries", type=int, help="ElevenLabs retries for 429/5xx")
    dub.add_argument("--language", help="TTS language hint")
    dub.add_argument("--output-dir", type=Path, help="fresh output directory")
    dub.add_argument("--ffmpeg", type=Path, help="FFmpeg executable path")
    dub.add_argument("--cache-dir", type=Path, help="opt in to persistent TTS cache")
    batch = subcommands.add_parser("dub-video", help="pause for subtitle review, or render reviewed subtitles per voice")
    batch.add_argument("video", type=Path, help="source video path")
    mode = batch.add_mutually_exclusive_group()
    mode.add_argument("--timeline", type=Path, help="reviewed subtitle directory/file; skips ASR")
    mode.add_argument("--accept-asr", action="store_true", help="explicitly accept ASR text and render immediately")
    choices = batch.add_mutually_exclusive_group(required=True)
    choices.add_argument("--voices", help="comma-separated explicit ElevenLabs voice IDs")
    choices.add_argument("--voices-file", type=Path, help="JSON array of VoiceProfile objects")
    batch.add_argument("--asr-model", default="base", help="Faster-Whisper model")
    batch.add_argument("--device", choices=["cpu", "cuda", "auto"], default="cpu")
    batch.add_argument("--compute-type", default="int8")
    batch.add_argument("--local-model-only", action="store_true")
    batch.add_argument("--model-id", help="default ElevenLabs model for --voices")
    batch.add_argument("--output-format", help="ElevenLabs MP3 or PCM format")
    batch.add_argument("--timeout", type=float, help="ElevenLabs request timeout in seconds")
    batch.add_argument("--max-retries", type=int, help="ElevenLabs retries for 429/5xx")
    batch.add_argument("--language", help="ASR and TTS language hint")
    batch.add_argument("--output-dir", type=Path, help="fresh batch output directory")
    batch.add_argument("--ffmpeg", type=Path, help="FFmpeg executable path")
    batch.add_argument("--ffprobe", type=Path, help="FFprobe executable path")
    add_asr_options(batch)
    cache = batch.add_mutually_exclusive_group()
    cache.add_argument("--cache-dir", type=Path, help="persistent TTS cache directory")
    cache.add_argument("--no-cache", action="store_true", help="disable persistent TTS cache")
    add_script_commands(subcommands)
    add_correction_commands(subcommands)
    return parser


def _elevenlabs_options(args: argparse.Namespace) -> dict:
    return {
        "output_format": args.output_format or os.environ.get("ELEVENLABS_OUTPUT_FORMAT") or "mp3_44100_128",
        "timeout": args.timeout if args.timeout is not None else float(os.environ.get("ELEVENLABS_TIMEOUT", "60")),
        "max_retries": args.max_retries if args.max_retries is not None else int(os.environ.get("ELEVENLABS_MAX_RETRIES", "2")),
        "ffmpeg_path": args.ffmpeg,
    }


def _batch_profiles(args: argparse.Namespace) -> tuple[VoiceProfile, ...]:
    default_model = args.model_id or os.environ.get("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2"
    if args.voices is not None:
        ids = [item.strip() for item in args.voices.split(",")]
        if not ids or any(not item for item in ids):
            raise ValidationError("--voices must contain non-empty voice IDs")
        profiles = tuple(VoiceProfile(item, item, "elevenlabs", item, default_model) for item in ids)
    else:
        try:
            data = json.loads(args.voices_file.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"cannot read voices JSON: {exc}") from exc
        if not isinstance(data, list):
            raise ValidationError("voices file must contain a JSON array")
        profiles = []
        for index, item in enumerate(data):
            if not isinstance(item, dict) or set(item) != {"id", "name", "provider", "voice_id", "model_id"}:
                raise ValidationError(f"voice {index} must contain id, name, provider, voice_id, model_id")
            profiles.append(VoiceProfile(**item))
        profiles = tuple(profiles)
    validate_voice_profiles(profiles)
    if any(profile.provider != "elevenlabs" for profile in profiles):
        raise ValidationError("dub-video currently supports ElevenLabs voice profiles only")
    return profiles


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    if args.command in ("script", "dub-scripts", "correction"):
        try:
            if args.command == "correction":
                return run_correction_command(args)
            return (run_script_command(args) if args.command == "script"
                    else run_dub_scripts(args, _elevenlabs_options(args)))
        except (MediaError, ProviderError, ValidationError, OSError, ValueError, TypeError) as exc:
            print(f"voice_dubbing: {exc}", file=sys.stderr)
            return 2
    if args.command == "validate-timeline":
        try:
            timeline = load_timeline(args.path)
        except ValidationError as exc:
            print(f"voice_dubbing: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({
            "valid": True,
            "segments": len(timeline.segments),
            "duration_ms": timeline.duration_ms,
        }, ensure_ascii=False))
        return 0
    if args.command == "transcribe-video":
        default_root = Path(__file__).resolve().parents[1] / "output"
        output_dir = args.output_dir or default_root / f"{args.video.stem[:40]}-{uuid4().hex[:8]}"
        try:
            provider = create_asr(args, output_dir, model=args.model)
            timeline = transcribe_video(
                args.video, provider=provider, output_dir=output_dir,
                language=args.language, ffmpeg_path=args.ffmpeg, ffprobe_path=args.ffprobe,
                cache_dir=asr_cache_directory(args),
            )
        except (MediaError, ProviderError, ValidationError, OSError, ValueError) as exc:
            print(f"voice_dubbing: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({
            "timeline": str((output_dir / "timeline.json").resolve()),
            "raw_timeline": str((output_dir / "raw_timeline.json").resolve()),
            "corrected_timeline": str((output_dir / "corrected_timeline.json").resolve()),
            "status": "awaiting_correction", "reviewed": False,
            "asr_calls": json.loads((output_dir / "asr_report.json").read_text(encoding="utf-8"))["asr_calls"],
            "segments": len(timeline.segments),
            "duration_ms": timeline.duration_ms,
            "provider": provider.provider_id,
        }, ensure_ascii=False))
        return 0
    if args.command == "dub-timeline":
        try:
            timeline, subtitle_source = resolve_tts_timeline(args.timeline)
            default_root = Path(__file__).resolve().parents[1] / "output"
            output_dir = args.output_dir or default_root / f"{args.timeline.stem[:40]}-dub-{uuid4().hex[:8]}"
            if args.provider == "fake":
                provider = FakeTTSProvider()
                voice_id = args.voice or "fake-default"
            else:
                provider = ElevenLabsTTSProvider(
                    model_id=args.model_id or os.environ.get("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2",
                    **_elevenlabs_options(args),
                )
                voice_id = args.voice or os.environ.get("ELEVENLABS_VOICE_ID")
                if not voice_id:
                    raise ValidationError("set --voice, ELEVENLABS_VOICE_ID, or --voice auto")
                if voice_id == "auto":
                    voice_id = provider.select_voice()
            if args.cache_dir:
                provider = CachedTTSProvider(provider, args.cache_dir)
            result = synthesize_timeline(
                timeline, provider=provider, voice_id=voice_id, output_dir=output_dir,
                language=args.language, ffmpeg_path=args.ffmpeg,
                subtitle_source=subtitle_source,
            )
        except (MediaError, ProviderError, ValidationError, OSError, ValueError) as exc:
            print(f"voice_dubbing: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({
            "audio": str(result.audio_path),
            "report_json": str(result.audio_path.parent / "dubbing_report.json"),
            "report_csv": str(result.audio_path.parent / "dubbing_report.csv"),
            "segments": len(result.segments),
            "duration_ms": result.output_duration_ms,
            "provider": result.provider_id,
            "voice_id": result.voice_id,
            "voice_name": getattr(provider, "selected_voice_name", None),
            "model_id": getattr(provider, "model_id", None),
            "output_format": getattr(provider, "output_format", None),
            "is_mock": result.is_mock,
            "warnings": list(result.warnings),
        }, ensure_ascii=False))
        return 0
    if args.command == "dub-video":
        try:
            profiles = _batch_profiles(args)
            options = _elevenlabs_options(args) if args.timeline is not None or args.accept_asr else {}
            return run_dub_video(args, profiles, options)
        except (MediaError, ProviderError, ValidationError, OSError, ValueError, TypeError) as exc:
            print(f"voice_dubbing: {exc}", file=sys.stderr)
            return 2
    return 2
