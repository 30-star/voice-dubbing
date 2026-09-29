"""Results of a single-voice audio dubbing run."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DubbingSegment:
    id: str
    start_ms: int
    end_ms: int
    text: str
    audio_path: Path
    audio_duration_ms: int
    target_duration_ms: int
    overrun_ms: int
    overlap_with_next_ms: int
    warnings: tuple[str, ...]
    playback_audio_path: Path | None = None
    playback_duration_ms: int | None = None
    speed_factor: float = 1.0
    playback_overlap_with_next_ms: int = 0

    @property
    def actual_duration_ms(self) -> int:
        return self.audio_duration_ms

    @property
    def overflow_ms(self) -> int:
        return self.audio_duration_ms - self.target_duration_ms


@dataclass(frozen=True, slots=True)
class AudioDubbingResult:
    provider_id: str
    voice_id: str
    is_mock: bool
    timeline_duration_ms: int
    output_duration_ms: int
    audio_path: Path
    segments: tuple[DubbingSegment, ...]
    warnings: tuple[str, ...]
