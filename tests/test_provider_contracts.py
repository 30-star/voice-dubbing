import tempfile
import unittest
from pathlib import Path

from voice_dubbing.asr import ASRProvider
from voice_dubbing.errors import ProviderError
from voice_dubbing.models import SpeechAudio, SpeechRequest, TranscriptSegment, TranscriptTimeline
from voice_dubbing.tts import TTSProvider


class FakeASR:
    provider_id = "fake-asr"

    def transcribe(self, audio_path: Path, *, language: str | None = None) -> TranscriptTimeline:
        return TranscriptTimeline([TranscriptSegment("1", 0, 500, f"{audio_path.name}:{language}")], 500)


class FailingASR:
    provider_id = "failing-asr"

    def transcribe(self, audio_path: Path, *, language: str | None = None) -> TranscriptTimeline:
        raise ProviderError("recognition failed")


class FakeTTS:
    provider_id = "fake-tts"

    def synthesize(self, request: SpeechRequest, *, output_dir: Path) -> SpeechAudio:
        return SpeechAudio(request.segment_id, output_dir / "fake.wav", 500, 24000, 1)


class FailingTTS:
    provider_id = "failing-tts"

    def synthesize(self, request: SpeechRequest, *, output_dir: Path) -> SpeechAudio:
        raise ProviderError("synthesis failed")


class ProviderContractTests(unittest.TestCase):
    def test_asr_adapters_are_interchangeable_and_error_is_preserved(self):
        providers: list[ASRProvider] = [FakeASR(), FailingASR()]
        self.assertEqual(providers[0].transcribe(Path("input.wav"), language="zh").segments[0].text,
                         "input.wav:zh")
        with self.assertRaisesRegex(ProviderError, "recognition failed"):
            providers[1].transcribe(Path("input.wav"))

    def test_tts_adapters_are_interchangeable_and_error_is_preserved(self):
        providers: list[TTSProvider] = [FakeTTS(), FailingTTS()]
        request = SpeechRequest("1", "你好", "voice")
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(providers[0].synthesize(request, output_dir=Path(folder)).duration_ms, 500)
            with self.assertRaisesRegex(ProviderError, "synthesis failed"):
                providers[1].synthesize(request, output_dir=Path(folder))


if __name__ == "__main__":
    unittest.main()
