"""ASR adapters receive extracted audio and return a provider-neutral timeline."""

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import TranscriptTimeline


class ASRProvider(Protocol):
    provider_id: str

    def transcribe(
        self, audio_path: Path, *, language: str | None = None
    ) -> TranscriptTimeline: ...


@runtime_checkable
class VideoASRProvider(Protocol):
    """Optional capability; existing audio adapters need not implement it."""
    def transcribe_video(self, video_path: Path, *, language: str | None = None) -> TranscriptTimeline: ...
