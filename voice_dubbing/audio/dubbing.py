"""Normalize sentence audio and place it on a sample-accurate WAV timeline."""

import subprocess
import shutil
import wave
from pathlib import Path

from ..errors import MediaError
from .extraction import _executable

SAMPLE_RATE = 24_000
SAMPLES_PER_MS = SAMPLE_RATE // 1000


def _run_ffmpeg(args: list[str], log_path: Path, *, timeout: int = 600) -> None:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
        log_path.write_text(result.stderr, encoding="utf-8")
    except (OSError, subprocess.TimeoutExpired) as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        raise MediaError(f"FFmpeg failed; see {log_path}: {exc}") from exc
    if result.returncode != 0:
        raise MediaError(f"FFmpeg exited {result.returncode}; see {log_path}: {result.stderr[-600:]}")


def wav_frames(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as audio:
            if (audio.getnchannels(), audio.getframerate(), audio.getsampwidth()) != (1, SAMPLE_RATE, 2):
                raise MediaError(f"expected mono 24 kHz PCM 16-bit WAV: {path}")
            return audio.getnframes()
    except (OSError, EOFError, wave.Error) as exc:
        raise MediaError(f"invalid WAV audio: {path}: {exc}") from exc


def normalize_speech(source: Path, target: Path, log_path: Path, *, ffmpeg_path: Path | None) -> int:
    if not source.is_file() or source.stat().st_size == 0:
        raise MediaError(f"TTS provider produced no audio: {source}")
    try:
        executable = _executable("ffmpeg", ffmpeg_path)
    except MediaError as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        raise
    # Provider/cache WAVs already have the mixing format. Verify the complete PCM
    # payload before copying; truncated/nonstandard audio keeps the existing decode path.
    frames = _standard_pcm_frames(source)
    if frames is not None:
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        log_path.write_text(f"Reused verified mono 24 kHz PCM16 WAV; frames={frames}; no conversion.\n", encoding="utf-8")
        return frames
    _run_ffmpeg([
        executable, "-nostdin", "-hide_banner", "-y", "-i", str(source),
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(target),
    ], log_path)
    frames = wav_frames(target)
    if frames == 0:
        raise MediaError(f"TTS provider produced empty audio: {source}")
    return frames


def _standard_pcm_frames(path: Path) -> int | None:
    try:
        with wave.open(str(path), "rb") as audio:
            if (audio.getnchannels(), audio.getframerate(), audio.getsampwidth(), audio.getcomptype()) != (1, SAMPLE_RATE, 2, "NONE"):
                return None
            frames = audio.getnframes()
            if frames <= 0:
                return None
            remaining = frames
            while remaining:
                count = min(remaining, SAMPLE_RATE)
                if len(audio.readframes(count)) != count * 2:
                    return None
                remaining -= count
            return frames
    except (OSError, EOFError, wave.Error):
        return None


def write_silence(path: Path, frames: int) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        remaining = frames
        while remaining:
            count = min(remaining, SAMPLE_RATE)
            audio.writeframesraw(b"\0\0" * count)
            remaining -= count


def mix_speech(
    clips: list[tuple[Path, int]], output: Path, total_frames: int,
    log_path: Path, *, ffmpeg_path: Path | None,
) -> None:
    if not clips:
        write_silence(output, total_frames)
        log_path.write_text("Silent timeline; FFmpeg mix not required.\n", encoding="utf-8")
        return
    try:
        executable = _executable("ffmpeg", ffmpeg_path)
    except MediaError as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        raise
    args = [executable, "-nostdin", "-hide_banner", "-y"]
    graph = []
    inputs = []
    for index, (path, start_ms) in enumerate(clips):
        args.extend(["-i", str(path)])
        graph.append(f"[{index}:a]adelay=delays={start_ms * SAMPLES_PER_MS}S:all=1[a{index}]")
        inputs.append(f"[a{index}]")
    graph.append(
        "".join(inputs)
        + f"amix=inputs={len(clips)}:duration=longest:normalize=0:dropout_transition=0,"
          "alimiter=limit=0.95:level=0:latency=1,"
        + f"apad,atrim=end_sample={total_frames}[out]"
    )
    args.extend([
        "-filter_complex", ";".join(graph), "-map", "[out]", "-ac", "1",
        "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(output),
    ])
    _run_ffmpeg(args, log_path)
    actual_frames = wav_frames(output)
    if actual_frames != total_frames:
        raise MediaError(f"FFmpeg produced {actual_frames} frames; expected {total_frames}")
