"""ASR selection exists only at the CLI composition boundary."""
from pathlib import Path
from .faster_whisper import FasterWhisperASRProvider
from .videocaptioner import VideoCaptionerASRProvider


def add_asr_options(parser):
    parser.add_argument("--asr-provider", choices=["videocaptioner", "faster-whisper"], default="videocaptioner")
    parser.add_argument("--videocaptioner-cli", type=Path)
    parser.add_argument("--asr-timeout", type=float, default=600)
    parser.add_argument("--asr-cache-dir", type=Path)
    parser.add_argument("--no-asr-cache", action="store_true")


def create_asr(args, output: Path, *, model: str):
    if args.asr_provider == "videocaptioner":
        return VideoCaptionerASRProvider(output_dir=output, cli_path=args.videocaptioner_cli,
            timeout=args.asr_timeout, ffmpeg_path=args.ffmpeg, ffprobe_path=args.ffprobe)
    return FasterWhisperASRProvider(model_size=model, device=args.device,
        compute_type=args.compute_type, local_files_only=args.local_model_only)


def asr_cache_directory(args) -> Path | None:
    return None if args.no_asr_cache else (args.asr_cache_dir or Path(__file__).resolve().parents[2] / "cache/asr")
