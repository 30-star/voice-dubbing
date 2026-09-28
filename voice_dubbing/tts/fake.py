"""Explicit synthetic test-tone provider; it does not generate spoken words."""

import math
import struct
import wave
from pathlib import Path

from ..models import SpeechAudio, SpeechRequest


class FakeTTSProvider:
    provider_id = "fake"
    is_mock = True

    def synthesize(self, request: SpeechRequest, *, output_dir: Path) -> SpeechAudio:
        output_dir.mkdir(parents=True, exist_ok=True)
        duration_ms = max(300, sum(not char.isspace() for char in request.text) * 250)
        sample_rate = 24_000
        samples = duration_ms * 24
        fade = sample_rate * 8 // 1000
        audio_path = output_dir / "speech.wav"
        with wave.open(str(audio_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            for start in range(0, samples, 4096):
                frames = bytearray()
                for index in range(start, min(start + 4096, samples)):
                    envelope = min(1.0, index / fade, (samples - 1 - index) / fade)
                    value = round(9000 * envelope * math.sin(2 * math.pi * 440 * index / sample_rate))
                    frames.extend(struct.pack("<h", value))
                output.writeframesraw(frames)
        return SpeechAudio(request.segment_id, audio_path, duration_ms, sample_rate, 1)
