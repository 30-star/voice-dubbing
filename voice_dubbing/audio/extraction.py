"""FFmpeg/FFprobe subprocess boundary for video-to-audio transcription."""

import json
import math
import shutil
import subprocess
from pathlib import Path

from ..errors import MediaError


def _executable(name: str, explicit: Path | None) -> str:
    if explicit is not None:
        if not explicit.is_file():
            raise MediaError(f"{name} executable not found: {explicit}")
        return str(explicit)
    found = shutil.which(name)
    if found:
        return found
    bundled = Path(__file__).resolve().parents[3] / "ffmpeg" / f"{name}.exe"
    if bundled.is_file():
        return str(bundled)
    raise MediaError(f"{name} executable not found; install it or pass --{name}")


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError(f"media command failed or timed out: {command[0]}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise MediaError(f"media command failed ({result.returncode}): {detail[-2000:]}")
    return result


def probe_video_duration_ms(video_path: Path, *, ffprobe_path: Path | None = None) -> int:
    executable = _executable("ffprobe", ffprobe_path)
    result = _run(
        [executable, "-v", "error", "-show_entries", "format=duration:stream=codec_type",
         "-of", "json", str(video_path)],
        timeout=30,
    )
    try:
        metadata = json.loads(result.stdout)
        duration = float(metadata["format"]["duration"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise MediaError("FFprobe did not return a valid video duration") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise MediaError("video duration must be greater than zero")
    if not any(stream.get("codec_type") == "audio" for stream in metadata.get("streams", [])):
        raise MediaError("source video has no audio track")
    return math.ceil(duration * 1000)


def extract_audio(
    video_path: Path, audio_path: Path, *, ffmpeg_path: Path | None = None
) -> None:
    executable = _executable("ffmpeg", ffmpeg_path)
    _run(
        [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(video_path), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(audio_path)],
        timeout=600,
    )
    if not audio_path.is_file() or audio_path.stat().st_size == 0:
        raise MediaError("FFmpeg completed without producing audio")
