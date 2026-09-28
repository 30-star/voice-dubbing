import tempfile
import unittest
from pathlib import Path

from voice_dubbing.errors import ProviderError, ValidationError
from voice_dubbing.models import SpeechRequest
from voice_dubbing.tts import CachedTTSProvider

from test_batch_dubbing import CountingTTS


class CacheTests(unittest.TestCase):
    def test_text_and_parameters_control_key_and_corruption_regenerates(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            provider = CountingTTS()
            request = SpeechRequest("one", "同一句话", "bill-id", "zh")
            first = CachedTTSProvider(provider, root / "cache", settings={"stability": 0.5})
            first.synthesize(request, output_dir=root / "one")
            second = CachedTTSProvider(provider, root / "cache", settings={"stability": 0.5})
            hit = second.synthesize(SpeechRequest("different-id", "同一句话", "bill-id", "zh"),
                                    output_dir=root / "two")
            self.assertEqual(provider.calls, 1)
            self.assertEqual(second.cache_hits, 1)
            self.assertEqual(hit.segment_id, "different-id")
            self.assertEqual(hit.audio_path.parent, root / "two")
            self.assertEqual((root / "two" / "speech.mp3").read_bytes(), b"raw audio fixture")

            changed = CachedTTSProvider(provider, root / "cache", settings={"stability": 0.6})
            changed.synthesize(request, output_dir=root / "three")
            self.assertEqual(provider.calls, 2)
            other_voice = CachedTTSProvider(provider, root / "cache", settings={"stability": 0.5})
            other_voice.synthesize(SpeechRequest("one", "同一句话", "sarah-id", "zh"),
                                   output_dir=root / "other-voice")
            provider.model_id = "another-model"
            other_model = CachedTTSProvider(provider, root / "cache", settings={"stability": 0.5})
            other_model.synthesize(request, output_dir=root / "other-model")
            self.assertEqual(provider.calls, 4)
            provider.model_id = "model"
            entry = second._entry(request)
            (entry / "speech.wav").write_bytes(b"corrupted")
            repaired = CachedTTSProvider(provider, root / "cache", settings={"stability": 0.5})
            repaired.synthesize(request, output_dir=root / "four")
            self.assertEqual(provider.calls, 5)
            self.assertEqual(repaired.cache_hits, 0)
            final = CachedTTSProvider(provider, root / "cache", settings={"stability": 0.5})
            final.synthesize(request, output_dir=root / "five")
            self.assertEqual(final.cache_hits, 1)

    def test_failed_provider_is_not_cached(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            provider = CountingTTS(fail=True)
            cache = CachedTTSProvider(provider, root / "cache")
            request = SpeechRequest("one", "失败", "bill-id")
            with self.assertRaises(ProviderError):
                cache.synthesize(request, output_dir=root / "one")
            provider.fail = False
            cache.synthesize(request, output_dir=root / "two")
            self.assertEqual(provider.calls, 2)
            self.assertEqual(cache.cache_hits, 0)

    def test_secret_cannot_enter_cache_parameters(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValidationError, "non-secret"):
                CachedTTSProvider(CountingTTS(), Path(folder), settings={"api_key": "private"})


if __name__ == "__main__":
    unittest.main()
