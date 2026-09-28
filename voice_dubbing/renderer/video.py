"""FFmpeg video replacement that preserves the complete dubbed speech tail."""

import json
import math
import os
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from ..audio.dubbing import SAMPLE_RATE, wav_frames
from ..audio.extraction import _executable
from ..errors import MediaError


@dataclass(frozen=True, slots=True)
class RenderedVideo:
    path: Path
    duration_ms: int
    audio_duration_ms: int
    width: int
    height: int


def _command(args: list[str], log_path: Path, *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout, check=False)
        log_path.write_text(result.stderr, encoding="utf-8")
    except (OSError, subprocess.TimeoutExpired) as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        raise MediaError(f"video command failed; see {log_path}: {exc}") from exc
    if result.returncode:
        raise MediaError(f"video command exited {result.returncode}; see {log_path}: {result.stderr[-500:]}")
    return result


def _probe(path: Path, ffprobe: str, log_path: Path) -> tuple[dict, dict, dict]:
    result = _command([ffprobe, "-v", "error", "-show_entries",
                       "format=duration:stream=codec_type,duration,width,height,avg_frame_rate",
                       "-of", "json", str(path)], log_path, timeout=60)
    try:
        info = json.loads(result.stdout)
        video = next(stream for stream in info["streams"] if stream.get("codec_type") == "video")
        audio = next(stream for stream in info["streams"] if stream.get("codec_type") == "audio")
        if int(video["width"]) <= 0 or int(video["height"]) <= 0:
            raise ValueError("invalid video dimensions")
        return info, video, audio
    except (ValueError, TypeError, KeyError, StopIteration) as exc:
        raise MediaError(f"FFprobe returned incomplete media streams: {path}") from exc


def _seconds(value: object, description: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MediaError(f"invalid {description} duration") from exc
    if not math.isfinite(result) or result <= 0:
        raise MediaError(f"invalid {description} duration")
    return result


def render_dubbed_video(
    video_path: Path, audio_path: Path, output_path: Path, *,
    ffmpeg_path: Path | None = None, ffprobe_path: Path | None = None,
) -> RenderedVideo:
    """Replace the source audio, hold its final frame if speech outlasts the picture."""
    if not video_path.is_file():
        raise MediaError(f"source video not found: {video_path}")
    if not audio_path.is_file():
        raise MediaError(f"dubbed audio not found: {audio_path}")
    if output_path.exists():
        raise MediaError(f"video output already exists: {output_path}")
    if output_path.parent.resolve() == video_path.parent.resolve():
        raise MediaError("video output must be outside the source directory")
    frames = wav_frames(audio_path)
    if not frames:
        raise MediaError("dubbed WAV is empty")
    audio_seconds = frames / SAMPLE_RATE
    output_path.parent.mkdir(parents=True, exist_ok=True)
    logs = output_path.parent / "logs"
    logs.mkdir(exist_ok=True)
    ffmpeg = _executable("ffmpeg", ffmpeg_path)
    ffprobe = _executable("ffprobe", ffprobe_path)
    source_info, source_video, _ = _probe(video_path, ffprobe, logs / "source-probe.log")
    source_video_seconds = _seconds(source_video.get("duration") or source_info["format"].get("duration"),
                                    "source video")
    source_format_seconds = _seconds(source_info["format"].get("duration"), "source media")
    try:
        frame_seconds = float(1 / Fraction(str(source_video["avg_frame_rate"])))
    except (KeyError, ValueError, ZeroDivisionError):
        frame_seconds = 1 / 30
    if not math.isfinite(frame_seconds) or frame_seconds <= 0:
        frame_seconds = 1 / 30
    target_seconds = max(audio_seconds, source_video_seconds, source_format_seconds)
    pad_seconds = max(0.0, target_seconds - source_video_seconds + frame_seconds)
    video_filter = (f"[0:v]tpad=stop_mode=clone:stop_duration={pad_seconds:.6f}[v]"
                    if target_seconds > source_video_seconds else "[0:v]null[v]")
    graph = f"{video_filter};[1:a]apad=whole_dur={target_seconds:.6f}[a]"
    partial = output_path.with_name(output_path.stem + ".partial.mp4")
    if partial.exists():
        raise MediaError(f"partial video already exists: {partial}")
    command = [ffmpeg, "-nostdin", "-hide_banner", "-y", "-i", str(video_path),
               "-i", str(audio_path), "-filter_complex", graph,
               "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium",
               "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
               "-movflags", "+faststart", str(partial)]
    try:
        _command(command, logs / "render.log", timeout=max(600, math.ceil(target_seconds * 10)))
        info, video, audio = _probe(partial, ffprobe, logs / "output-probe.log")
        rendered_video_seconds = _seconds(video.get("duration") or info["format"].get("duration"),
                                          "rendered video")
        rendered_audio_seconds = _seconds(audio.get("duration") or info["format"].get("duration"),
                                          "rendered audio")
        if (int(video["width"]), int(video["height"])) != (
                int(source_video["width"]), int(source_video["height"])):
            raise MediaError("rendered video dimensions changed")
        # One frame and two AAC frames account for container time-base/codec rounding.
        if rendered_video_seconds + frame_seconds + 0.005 < max(audio_seconds, source_video_seconds):
            raise MediaError("rendered picture does not cover the full speech tail")
        if rendered_audio_seconds + 0.05 < audio_seconds:
            raise MediaError("rendered audio is shorter than the dubbed WAV")
        _command([ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-i", str(partial),
                  "-map", "0:v:0", "-map", "0:a:0", "-f", "null", os.devnull],
                 logs / "decode-check.log", timeout=max(600, math.ceil(target_seconds * 10)))
        partial.replace(output_path)
        return RenderedVideo(output_path, math.ceil(rendered_video_seconds * 1000),
                             math.ceil(rendered_audio_seconds * 1000),
                             int(video["width"]), int(video["height"]))
    except Exception:
        # Retain the partial render and its logs for diagnosis; never publish it as output.
        raise
