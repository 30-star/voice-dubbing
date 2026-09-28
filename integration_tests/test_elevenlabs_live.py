"""Explicit, paid ElevenLabs acceptance using the existing two-sentence timeline."""

import contextlib
import difflib
import io
import json
import math
import os
import unittest
from pathlib import Path
from uuid import uuid4

from voice_dubbing.asr import FasterWhisperASRProvider
from voice_dubbing.audio.dubbing import wav_frames
from voice_dubbing.cli import main
from voice_dubbing.correction import initialize_correction, save_correction
from voice_dubbing.timeline import load_timeline


ROOT = Path(__file__).resolve().parents[1]
TIMELINE = Path(os.environ.get("VOICE_DUBBING_TIMELINE", "fixtures/timeline.json"))


class ElevenLabsLiveTests(unittest.TestCase):
    def test_existing_timeline_generates_real_audio(self):
        if os.environ.get("VOICE_DUBBING_RUN_INTEGRATION") != "1":
            self.skipTest("set VOICE_DUBBING_RUN_INTEGRATION=1 to make paid API requests")
        if not os.environ.get("ELEVENLABS_API_KEY"):
            self.skipTest("ELEVENLABS_API_KEY is not configured")
        timeline = Path(os.environ.get("VOICE_DUBBING_TIMELINE", str(TIMELINE)))
        self.assertTrue(timeline.is_file(), f"acceptance timeline missing: {timeline}")
        review_dir = ROOT / "output" / f"live-single-review-{uuid4().hex[:8]}"
        initialize_correction(load_timeline(timeline), review_dir)
        save_correction(review_dir)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = main(["dub-timeline", str(review_dir), "--provider", "elevenlabs", "--voice", "auto"])
        self.assertEqual(code, 0)
        result = json.loads(stream.getvalue())
        report = json.loads(Path(result["report_json"]).read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "succeeded")
        self.assertFalse(report["is_mock"])
        self.assertEqual(report["timeline_duration_ms"], 4480)
        self.assertEqual(len(report["segments"]), 2)
        self.assertEqual([row["target_duration_ms"] for row in report["segments"]], [2320, 2160])
        self.assertEqual([row["overflow_ms"] for row in report["segments"]], [
            row["actual_duration_ms"] - row["target_duration_ms"] for row in report["segments"]
        ])
        self.assertEqual(math.ceil(wav_frames(Path(result["audio"])) / 24), report["output_duration_ms"])
        for row in report["segments"]:
            speech = Path(row["audio_path"])
            self.assertTrue(speech.is_file())
            self.assertGreater(wav_frames(speech), 0)
            original_name = "speech.pcm" if report["output_format"].startswith("pcm_") else "speech.mp3"
            self.assertTrue((speech.parent / original_name).is_file())
        checks = []
        asr = FasterWhisperASRProvider(model_size="base", local_files_only=True)
        for row in report["segments"]:
            try:
                recognized = "".join(part.text for part in asr.transcribe(Path(row["audio_path"]), language="zh").segments)
                expected = "".join(row["text"].split())
                observed = "".join(recognized.split())
                checks.append({
                    "id": row["id"], "expected": row["text"], "recognized": recognized,
                    "similarity": round(difflib.SequenceMatcher(None, expected, observed).ratio(), 3),
                })
            except Exception as exc:
                checks.append({"id": row["id"], "asr_error": str(exc)})
        (Path(result["audio"]).parent / "tts_asr_check.json").write_text(
            json.dumps(checks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps({"output_dir": str(Path(result["audio"]).parent), "report": report}, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
