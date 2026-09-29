"""Fit complete PCM speech into its window without moving subtitle timestamps."""

import math
from dataclasses import dataclass
from pathlib import Path

from ..audio.dubbing import SAMPLES_PER_MS, _run_ffmpeg, wav_frames
from ..audio.extraction import _executable
from ..errors import MediaError, ValidationError

DURATION_TOLERANCE_MS = 150


@dataclass(frozen=True, slots=True)
class MatchedSpeech:
    path: Path
    frames: int
    speed_factor: float = 1.0


def tempo_filter(speed: float) -> str:
    """Chain factors <=2; FFmpeg warns that larger single factors skip samples."""
    if not math.isfinite(speed) or speed < 1:
        raise ValidationError("speech speed must be finite and at least 1")
    factors = []
    while speed > 2:
        factors.append("atempo=2")
        speed /= 2
    factors.append(f"atempo={speed:.10f}")
    return ",".join(factors)


def match_speech(source: Path, target: Path, window_ms: int, log_path: Path, *,
                 ffmpeg_path: Path | None = None) -> MatchedSpeech:
    """Only speed up overflows >150ms. Never trim, slow down or overwrite input."""
    if window_ms <= 0:
        raise ValidationError("speech window must be positive; overlapping starts cannot be aligned")
    frames = wav_frames(source)
    if frames <= 0:
        raise MediaError(f"empty speech audio: {source}")
    window_frames = window_ms * SAMPLES_PER_MS
    if frames - window_frames <= DURATION_TOLERANCE_MS * SAMPLES_PER_MS:
        return MatchedSpeech(source, frames)
    if source.resolve() == target.resolve() or target.exists():
        raise MediaError(f"duration matching cannot overwrite existing audio: {target}")
    # Small headroom accommodates atempo's block rounding. Measured output,
    # rather than the nominal ratio, decides whether the complete clip fits.
    desired_frames = max(1, window_frames - min(10 * SAMPLES_PER_MS, window_frames // 20))
    speed = frames / desired_frames
    ffmpeg = _executable("ffmpeg", ffmpeg_path)
    partial = target.with_name(target.stem + ".partial.wav")
    if partial.exists():
        raise MediaError(f"duration matching partial already exists: {partial}")
    for attempt in range(1, 4):
        attempt_log = log_path.with_name(f"{log_path.stem}-{attempt}{log_path.suffix}")
        _run_ffmpeg([ffmpeg, "-nostdin", "-hide_banner", "-y", "-i", str(source),
                     "-af", tempo_filter(speed), "-ac", "1", "-ar", "24000",
                     "-c:a", "pcm_s16le", str(partial)], attempt_log)
        measured = wav_frames(partial)
        if 0 < measured <= window_frames:
            partial.replace(target)
            log_path.write_text(f"speed={speed:.10f}; original_frames={frames}; "
                                f"playback_frames={measured}; window_frames={window_frames}\n",
                                encoding="utf-8")
            return MatchedSpeech(target, measured, speed)
        if measured <= 0:
            raise MediaError(f"duration matching produced empty audio; see {attempt_log}")
        # Retry local processing from the original, never another TTS request.
        speed *= measured / desired_frames
    raise MediaError(f"complete speech cannot fit its window; see {log_path.parent}")
