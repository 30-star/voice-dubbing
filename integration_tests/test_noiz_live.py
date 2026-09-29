"""One explicitly enabled, potentially billable Noiz request; not an offline test."""

import os
import unittest
from pathlib import Path
from uuid import uuid4

from voice_dubbing.audio.dubbing import wav_frames
from voice_dubbing.models import SpeechRequest
from voice_dubbing.tts import NoizTTSProvider


@unittest.skipUnless(os.environ.get("VOICE_DUBBING_RUN_NOIZ_INTEGRATION") == "1",
                     "Noiz integration not explicitly enabled")
class NoizLiveTests(unittest.TestCase):
    def test_one_real_chinese_sentence(self):
        if not os.environ.get("NOIZ_API_KEY") or not os.environ.get("NOIZ_VOICE_ID"):
            self.skipTest("NOIZ_API_KEY and an explicitly selected NOIZ_VOICE_ID are required")
        output = Path(__file__).resolve().parents[1] / "output" / ("noiz-live-" + uuid4().hex[:8])
        provider = NoizTTSProvider(max_retries=0)
        audio = provider.synthesize(SpeechRequest("1", "这是智能配音的中文接口测试。",
                                                  os.environ["NOIZ_VOICE_ID"], "zh"), output_dir=output)
        self.assertGreater(wav_frames(audio.audio_path), 0)
        self.assertEqual(provider.synthesis_http_requests, 1)
        self.assertTrue((output / "speech.original.wav").is_file())
        print("Noiz integration audio:", audio.audio_path)
