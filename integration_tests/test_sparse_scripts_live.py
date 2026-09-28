"""Explicit live acceptance: no ASR/extraction, pay only for missing new texts."""
import contextlib
import hashlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from voice_dubbing.cli import main
from voice_dubbing.correction.store import resolve_corrected_timeline
from voice_dubbing.errors import ProviderError
from voice_dubbing.models import SpeechRequest
from voice_dubbing.tts import CachedTTSProvider, ElevenLabsTTSProvider

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get("VOICE_DUBBING_SOURCE_VIDEO", "fixtures/source.mp4"))
BASE = Path(os.environ.get("VOICE_DUBBING_CORRECTED_DIR", "fixtures/subtitles"))
NEW_TEXTS = {"variant_a": ("1", "连这本好书都舍不得买"),
             "variant_b": ("2", "那你这辈子基本就这样了")}
VOICES = [
    {"id": "bill", "name": "Bill", "provider": "elevenlabs",
     "voice_id": "pqHfZKP75CvOlQylNhV4", "model_id": "eleven_multilingual_v2"},
    {"id": "sarah", "name": "Sarah", "provider": "elevenlabs",
     "voice_id": "EXAVITQu4vr4xnSDxMaL", "model_id": "eleven_multilingual_v2"},
]


class NewTextsOnly(ElevenLabsTTSProvider):
    def synthesize(self, request, *, output_dir):
        if request.text not in {text for _, text in NEW_TEXTS.values()}:
            raise ProviderError("acceptance forbids paid regeneration of base sentences")
        return super().synthesize(request, output_dir=output_dir)


class SparseScriptLiveTests(unittest.TestCase):
    def invoke(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([str(a) for a in args])
        self.assertEqual(code, 0, stderr.getvalue() + stdout.getvalue())
        return json.loads(stdout.getvalue())

    def test_three_sparse_variants_two_real_voices_without_asr(self):
        if os.environ.get("VOICE_DUBBING_RUN_SPARSE_SCRIPT_INTEGRATION") != "1":
            self.skipTest("explicit VOICE_DUBBING_RUN_SPARSE_SCRIPT_INTEGRATION=1 required")
        if not os.environ.get("ELEVENLABS_API_KEY"):
            self.skipTest("ELEVENLABS_API_KEY unavailable")
        self.assertTrue(SOURCE.is_file())
        base, _ = resolve_corrected_timeline(BASE)
        before = {name: (BASE / name).read_bytes() for name in ("raw_timeline.json", "corrected_timeline.json")}
        misses = 0
        for voice in VOICES:
            cache = CachedTTSProvider(ElevenLabsTTSProvider(model_id=voice["model_id"],
                output_format="mp3_44100_128", max_retries=0), ROOT / "cache" / "tts")
            for segment in base.segments:
                self.assertTrue(cache.is_cached(SpeechRequest(segment.id, segment.text, voice["voice_id"], "zh")),
                                "base cache missing: stop before paid requests")
            for segment_id, text in NEW_TEXTS.values():
                misses += not cache.is_cached(SpeechRequest(segment_id, text, voice["voice_id"], "zh"))
        output = ROOT / "output" / f"sparse-script-acceptance-{uuid4().hex[:8]}"
        scripts = output / "scripts"
        original = scripts / "original.json"
        self.invoke(["script", "create", BASE, "--id", "original", "--name", "原版", "--output", original])
        for id, (segment_id, text) in NEW_TEXTS.items():
            path = scripts / (id + ".json")
            self.invoke(["script", "copy", original, "--id", id, "--name",
                         "版本A" if id == "variant_a" else "版本B", "--output", path])
            self.invoke(["script", "set-text", path, "--segment-id", segment_id, "--text", text])
        jobs = scripts / "jobs.json"
        jobs.write_text(json.dumps([{"script": id + ".json", "voices": VOICES}
            for id in ("original", "variant_a", "variant_b")], ensure_ascii=False, indent=2), encoding="utf-8")
        with patch("voice_dubbing.asr.FasterWhisperASRProvider.transcribe", side_effect=AssertionError("ASR forbidden")) as asr, \
                patch("voice_dubbing.pipeline.extract_audio", side_effect=AssertionError("extraction forbidden")) as extract, \
                patch("voice_dubbing.script_cli.ElevenLabsTTSProvider", NewTextsOnly):
            result = self.invoke(["dub-scripts", SOURCE, "--timeline", BASE, "--variants-file", jobs,
                "--language", "zh", "--output-format", "mp3_44100_128", "--max-retries", "0",
                "--output-dir", output / "results"])
        asr.assert_not_called()
        extract.assert_not_called()
        report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
        self.assertEqual(report["asr_calls"], 0)
        self.assertEqual(report["audio_extraction_calls"], 0)
        self.assertEqual(report["cache_hits"], 12 - misses)
        self.assertEqual(report["generated_segments"], misses)
        self.assertEqual(report["supplier_requests"], misses)
        self.assertEqual(len(report["combinations"]), 6)
        details = []
        for item in report["combinations"]:
            self.assertEqual(item["status"], "succeeded")
            video = Path(item["video_path"])
            self.assertTrue(video.is_file())
            self.assertEqual((video.parent / "logs" / "decode-check.log").read_text(encoding="utf-8"), "")
            audio_report = json.loads((video.parent / "dubbing_report.json").read_text(encoding="utf-8"))
            self.assertFalse(audio_report["is_mock"])
            self.assertEqual(audio_report["script_variant"]["name"], item["script_name"])
            self.assertEqual(audio_report["script_variant"]["voice_name"], item["voice"]["name"])
            expected = {s.id: s.text for s in base.segments}
            if item["script_id"] in NEW_TEXTS:
                id, text = NEW_TEXTS[item["script_id"]]
                expected[id] = text
            for segment in audio_report["segments"]:
                self.assertEqual(segment["text"], expected[segment["id"]])
                original_segment = next(s for s in base.segments if s.id == segment["id"])
                self.assertEqual((segment["start_ms"], segment["end_ms"]),
                                 (original_segment.start_ms, original_segment.end_ms))
                if segment["text"] == original_segment.text:
                    self.assertTrue(segment["cache_hit"])
                    original_audio = output / "results" / "variants" / "original" / item["voice"]["id"]
                    sentence_dir = "000001" if segment["id"] == "1" else "000002"
                    self.assertEqual((original_audio / "segments" / sentence_dir / "speech.mp3").read_bytes(),
                                     (video.parent / "segments" / sentence_dir / "speech.mp3").read_bytes())
            self.assertGreaterEqual(item["video_duration_ms"] + 50, audio_report["output_duration_ms"])
            details.append({"variant": item["script_name"], "voice": item["voice"]["name"],
                "video": str(video), "audio_duration_ms": audio_report["output_duration_ms"],
                "video_duration_ms": item["video_duration_ms"], "segments": audio_report["segments"]})
        for name, data in before.items():
            self.assertEqual((BASE / name).read_bytes(), data)
        check = {"asr_calls": 0, "audio_extraction_calls": 0, "all_mp4_decodable": True,
            "base_files_unchanged": True, "cache_hits": report["cache_hits"],
            "generated_segments": misses, "http_requests": report["supplier_requests"],
            "input_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in before.items()},
            "original_video_duration_ms": base.duration_ms, "subjective_listening": "pending_user_review",
            "combinations": details}
        (output / "acceptance_check.json").write_text(json.dumps(check, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"acceptance": str(output), "cache_hits": report["cache_hits"],
                          "new_segments": misses, "http_requests": report["supplier_requests"]}, ensure_ascii=False))
