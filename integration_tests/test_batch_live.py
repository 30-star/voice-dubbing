"""Opt-in paid batch acceptance; ordinary tests never call ElevenLabs."""

import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from uuid import uuid4

from voice_dubbing.cli import main


ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path("input.mp4")
BILL = "pqHfZKP75CvOlQylNhV4"
SARAH = "EXAVITQu4vr4xnSDxMaL"


class BatchLiveTests(unittest.TestCase):
    def test_one_real_asr_and_two_real_voices(self):
        if os.environ.get("VOICE_DUBBING_RUN_BATCH_INTEGRATION") != "1":
            self.skipTest("set VOICE_DUBBING_RUN_BATCH_INTEGRATION=1 for paid batch acceptance")
        if not os.environ.get("ELEVENLABS_API_KEY"):
            self.skipTest("ELEVENLABS_API_KEY is not configured")
        source = Path(os.environ.get("VOICE_DUBBING_SOURCE_VIDEO", str(SOURCE)))
        self.assertTrue(source.is_file(), f"source video missing: {source}")
        task_id = uuid4().hex[:8]
        output = ROOT / "output" / f"live-batch-{task_id}"
        cache = ROOT / "cache" / f"live-batch-{task_id}"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["dub-video", str(source), "--voices", f"{BILL},{SARAH}",
                         "--language", "zh", "--local-model-only", "--accept-asr",
                         "--output-dir", str(output), "--cache-dir", str(cache)])
        self.assertEqual(code, 0)
        result = json.loads(stdout.getvalue())
        report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
        self.assertEqual(report["asr_calls"], 1)
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(report["cache_hits"], 0)
        self.assertEqual(report["supplier_requests"], 4)
        self.assertEqual([voice["voice"]["voice_id"] for voice in report["voices"]], [BILL, SARAH])
        for voice in report["voices"]:
            self.assertTrue(Path(voice["audio_path"]).is_file())
            self.assertTrue(Path(voice["video_path"]).is_file())
            details = json.loads((Path(voice["audio_path"]).parent / "dubbing_report.json").read_text(encoding="utf-8"))
            self.assertEqual(len(details["segments"]), 2)
            self.assertFalse(details["is_mock"])
        print(json.dumps({"report": str(Path(result["report"])),
                          "videos": [voice["video_path"] for voice in report["voices"]]}, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
