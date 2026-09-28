"""TTS adapters write actual audio and report its measured properties."""

from pathlib import Path
from typing import Protocol

from ..models import SpeechAudio, SpeechRequest


class TTSProvider(Protocol):
    provider_id: str

    def synthesize(self, request: SpeechRequest, *, output_dir: Path) -> SpeechAudio: ...
