import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from voice_dubbing.asr import FasterWhisperASRProvider
from voice_dubbing.errors import ProviderError


class StubModel:
    def __init__(self, segments, duration=3.0):
        self.segments = segments
        self.duration = duration
        self.calls = []

    def transcribe(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return iter(self.segments), SimpleNamespace(duration=self.duration)


class FasterWhisperProviderTests(unittest.TestCase):
    def test_segments_convert_to_timeline_with_stable_ids(self):
        model = StubModel([
            SimpleNamespace(start=0.1, end=0.9, text=" 你好 "),
            SimpleNamespace(start=1.0, end=1.2, text=" "),
            SimpleNamespace(start=1.5, end=2.2, text="世界"),
        ])
        provider = FasterWhisperASRProvider()
        provider._model = model
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / "speech.wav"
            audio.touch()
            timeline = provider.transcribe(audio, language="zh")
        self.assertEqual([(s.id, s.start_ms, s.end_ms, s.text) for s in timeline.segments],
                         [("1", 100, 900, "你好"), ("2", 1500, 2200, "世界")])
        self.assertEqual(timeline.duration_ms, 3000)
        self.assertEqual(model.calls[0][1]["language"], "zh")

    def test_generator_failure_is_wrapped(self):
        class BrokenModel:
            def transcribe(self, path, **kwargs):
                def broken():
                    raise RuntimeError("decoder failed")
                    yield
                return broken(), SimpleNamespace(duration=1.0)

        provider = FasterWhisperASRProvider()
        provider._model = BrokenModel()
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / "speech.wav"
            audio.touch()
            with self.assertRaisesRegex(ProviderError, "decoder failed"):
                provider.transcribe(audio)

    def test_empty_speech_and_bad_timestamps(self):
        provider = FasterWhisperASRProvider()
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / "silence.wav"
            audio.touch()
            provider._model = StubModel([], duration=2.5)
            self.assertEqual(provider.transcribe(audio).duration_ms, 2500)
            provider._model = StubModel([SimpleNamespace(start=0.5, end=0.5, text="bad")])
            with self.assertRaisesRegex(ProviderError, "no duration"):
                provider.transcribe(audio)

    def test_auto_language_is_passed_as_detection(self):
        model = StubModel([])
        provider = FasterWhisperASRProvider()
        provider._model = model
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / "speech.wav"
            audio.touch()
            provider.transcribe(audio, language="auto")
        self.assertIsNone(model.calls[0][1]["language"])

    def test_missing_audio_is_clear_error(self):
        with self.assertRaisesRegex(ProviderError, "audio file not found"):
            FasterWhisperASRProvider().transcribe(Path("missing.wav"))


if __name__ == "__main__":
    unittest.main()
