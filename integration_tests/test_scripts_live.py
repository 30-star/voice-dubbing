"""Explicit paid acceptance; unchanged sentences MUST use the existing cache."""

import contextlib
import hashlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from voice_dubbing.asr.faster_whisper import FasterWhisperASRProvider
from voice_dubbing.cli import main
from voice_dubbing.correction import save_correction
from voice_dubbing.errors import ProviderError
from voice_dubbing.models import SpeechRequest
from voice_dubbing.timeline import load_timeline
from voice_dubbing.tts import CachedTTSProvider, ElevenLabsTTSProvider


ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path("input.mp4")
PRIOR = ROOT / "output" / "··217312-batch-f4e96e12" / "timeline.json"
REVISED_TEXT = "那你这辈子基本就定型了"
VOICES = [
    {"id": "bill", "name": "Bill", "provider": "elevenlabs",
     "voice_id": "pqHfZKP75CvOlQylNhV4", "model_id": "eleven_multilingual_v2"},
    {"id": "sarah", "name": "Sarah", "provider": "elevenlabs",
     "voice_id": "EXAVITQu4vr4xnSDxMaL", "model_id": "eleven_multilingual_v2"},
]


class ChangedSentenceOnly(ElevenLabsTTSProvider):
    def synthesize(self, request, *, output_dir):
        if request.text != REVISED_TEXT:
            raise ProviderError("acceptance forbids generating unchanged sentences: required cache missing")
        return super().synthesize(request, output_dir=output_dir)


class ScriptLiveTests(unittest.TestCase):
    def invoke(self, arguments):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(arguments)
        self.assertEqual(code, 0, stdout.getvalue())
        return json.loads(stdout.getvalue())

    def test_one_asr_two_scripts_two_real_voices(self):
        if os.environ.get("VOICE_DUBBING_RUN_SCRIPT_INTEGRATION") != "1":
            self.skipTest("set VOICE_DUBBING_RUN_SCRIPT_INTEGRATION=1 for paid script acceptance")
        if not os.environ.get("ELEVENLABS_API_KEY"):
            self.skipTest("ELEVENLABS_API_KEY is not configured")
        self.assertTrue(SOURCE.is_file())
        prior_bytes = PRIOR.read_bytes()
        prior = load_timeline(PRIOR)
        expected_generated = 0
        for voice in VOICES:
            cache = CachedTTSProvider(ElevenLabsTTSProvider(model_id=voice["model_id"],
                output_format="mp3_44100_128"), ROOT / "cache" / "tts")
            for segment in prior.segments:
                self.assertTrue(cache.is_cached(SpeechRequest(segment.id, segment.text, voice["voice_id"], "zh")),
                                "original voice cache missing; no unchanged paid request is allowed")
            expected_generated += not cache.is_cached(SpeechRequest("2", REVISED_TEXT, voice["voice_id"], "zh"))

        output = ROOT / "output" / f"script-acceptance-{uuid4().hex[:8]}"
        asr_calls = []
        original_transcribe = FasterWhisperASRProvider.transcribe

        def counted_transcribe(provider, audio_path, **kwargs):
            asr_calls.append(str(audio_path))
            return original_transcribe(provider, audio_path, **kwargs)

        with patch.object(FasterWhisperASRProvider, "transcribe", counted_transcribe):
            self.invoke(["transcribe-video", str(SOURCE), "--language", "zh", "--local-model-only",
                         "--output-dir", str(output / "source")])
            timeline_path = output / "source" / "timeline.json"
            source_bytes = timeline_path.read_bytes()
            save_correction(output / "source")
            self.assertEqual(load_timeline(timeline_path), prior,
                             "new ASR differs from cached acceptance timeline; stop before TTS")
            scripts = output / "scripts"
            original_path, revised_path = scripts / "original.json", scripts / "revised.json"
            self.invoke(["script", "create", str(timeline_path), "--id", "original", "--name", "原版",
                         "--output", str(original_path)])
            self.invoke(["script", "copy", str(original_path), "--id", "revised", "--name", "修改版",
                         "--output", str(revised_path)])
            self.invoke(["script", "set-text", str(revised_path), "--segment-id", "2", "--text", REVISED_TEXT])
            jobs = scripts / "jobs.json"
            jobs.write_text(json.dumps([
                {"script": "original.json", "voices": VOICES},
                {"script": "revised.json", "voices": VOICES},
            ], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            with patch("voice_dubbing.tts.elevenlabs.ElevenLabsTTSProvider", ChangedSentenceOnly):
                result = self.invoke(["dub-scripts", str(SOURCE), "--timeline", str(timeline_path),
                    "--variants-file", str(jobs), "--language", "zh", "--output-format", "mp3_44100_128",
                    "--max-retries", "0", "--output-dir", str(output / "results")])

        report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
        self.assertEqual(len(asr_calls), 1)
        self.assertEqual(report["asr_calls"], 0)
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(len(report["combinations"]), 4)
        self.assertEqual(report["cache_hits"], 8 - expected_generated)
        self.assertEqual(report["generated_segments"], expected_generated)
        self.assertEqual(report["supplier_requests"], expected_generated)
        self.assertEqual(timeline_path.read_bytes(), source_bytes)
        self.assertEqual(PRIOR.read_bytes(), prior_bytes)
        details = []
        for item in report["combinations"]:
            audio = Path(item["audio_path"])
            self.assertTrue(Path(item["video_path"]).is_file())
            voice_report = json.loads((audio.parent / "dubbing_report.json").read_text(encoding="utf-8"))
            self.assertFalse(voice_report["is_mock"])
            self.assertTrue(voice_report["segments"][0]["cache_hit"])
            expected_text = REVISED_TEXT if item["script_id"] == "revised" else prior.segments[1].text
            self.assertEqual(voice_report["segments"][1]["text"], expected_text)
            details.append({"script_id": item["script_id"], "voice": item["voice"]["name"],
                            "segments": voice_report["segments"],
                            "audio_duration_ms": voice_report["output_duration_ms"],
                            "video_path": item["video_path"]})
        for voice in VOICES:
            original_dir = output / "results" / "variants" / "original" / voice["id"]
            revised_dir = output / "results" / "variants" / "revised" / voice["id"]
            for filename in ("speech.mp3", "speech.wav", "normalized.wav"):
                self.assertEqual((original_dir / "segments" / "000001" / filename).read_bytes(),
                                 (revised_dir / "segments" / "000001" / filename).read_bytes())
            self.assertNotEqual((original_dir / "segments" / "000002" / "speech.mp3").read_bytes(),
                                (revised_dir / "segments" / "000002" / "speech.mp3").read_bytes())
        check = {"asr_calls": len(asr_calls), "dubbing_asr_calls": report["asr_calls"],
                 "timeline_file_sha256_before": hashlib.sha256(source_bytes).hexdigest(),
                 "timeline_file_sha256_after": hashlib.sha256(timeline_path.read_bytes()).hexdigest(),
                 "prior_timeline_unchanged": PRIOR.read_bytes() == prior_bytes,
                 "cache_hits": report["cache_hits"], "generated_segments": expected_generated,
                 "supplier_requests": report["supplier_requests"], "combinations": details}
        (output / "acceptance_check.json").write_text(json.dumps(check, ensure_ascii=False, indent=2) + "\n",
                                                     encoding="utf-8")
        print(json.dumps({"acceptance": str(output), "report": result["report"],
                          "cache_hits": report["cache_hits"], "generated": expected_generated}, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
