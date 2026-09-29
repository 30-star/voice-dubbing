"""Opt-in paid baseline-correction acceptance with an isolated seeded cache."""

import contextlib
import hashlib
import io
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from voice_dubbing.asr import FasterWhisperASRProvider
from voice_dubbing.cli import main
from voice_dubbing.correction import load_saved
from voice_dubbing.errors import ProviderError
from voice_dubbing.models import SpeechRequest
from voice_dubbing.timeline import load_timeline
from voice_dubbing.tts import CachedTTSProvider, ElevenLabsTTSProvider


ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path("input.mp4")
PRIOR = ROOT / "output" / "script-acceptance-3b09597b" / "source" / "timeline.json"
CORRECTED_TEXT = "那你这辈子基本就定型了"
VOICES = [
    {"id": "bill", "name": "Bill", "provider": "elevenlabs", "voice_id": "pqHfZKP75CvOlQylNhV4",
     "model_id": "eleven_multilingual_v2"},
    {"id": "sarah", "name": "Sarah", "provider": "elevenlabs", "voice_id": "EXAVITQu4vr4xnSDxMaL",
     "model_id": "eleven_multilingual_v2"},
]


class ChangedSentenceOnly(ElevenLabsTTSProvider):
    def synthesize(self, request, *, output_dir):
        if request.text != CORRECTED_TEXT:
            raise ProviderError("acceptance forbids generating unchanged subtitles; required cache missing")
        return super().synthesize(request, output_dir=output_dir)


def fingerprints(folder: Path) -> dict:
    return {str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in folder.rglob("*") if path.is_file()}


class CorrectionLiveTests(unittest.TestCase):
    def invoke(self, arguments):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(arguments)
        self.assertEqual(code, 0, stdout.getvalue())
        return json.loads(stdout.getvalue())

    def test_saved_correction_two_voices_without_repeating_asr(self):
        if os.environ.get("VOICE_DUBBING_RUN_CORRECTION_INTEGRATION") != "1":
            self.skipTest("set VOICE_DUBBING_RUN_CORRECTION_INTEGRATION=1 for paid correction acceptance")
        if not os.environ.get("ELEVENLABS_API_KEY"):
            self.skipTest("ELEVENLABS_API_KEY is not configured")
        self.assertTrue(SOURCE.is_file())
        prior_bytes = PRIOR.read_bytes()
        prior = load_timeline(PRIOR)
        output = ROOT / "output" / f"correction-acceptance-{uuid4().hex[:8]}"
        output.mkdir(parents=True)
        cache_dir = output / "cache" / "tts"
        cache_before = fingerprints(ROOT / "cache" / "tts")
        # Explicitly seed only verified original text entries into this acceptance cache.
        # The production cache (including previous corrected speech) is read-only here.
        for voice in VOICES:
            provider = ElevenLabsTTSProvider(model_id=voice["model_id"], output_format="mp3_44100_128")
            existing = CachedTTSProvider(provider, ROOT / "cache" / "tts")
            isolated = CachedTTSProvider(provider, cache_dir)
            for segment in prior.segments:
                request = SpeechRequest(segment.id, segment.text, voice["voice_id"], "zh")
                self.assertTrue(existing.is_cached(request), "original cache missing; stop before paid requests")
                shutil.copytree(existing._entry(request), isolated._entry(request))
                self.assertTrue(isolated.is_cached(request))
            self.assertFalse(isolated.is_cached(SpeechRequest("2", CORRECTED_TEXT, voice["voice_id"], "zh")))

        calls = []
        original_transcribe = FasterWhisperASRProvider.transcribe
        def counted(provider, audio_path, **kwargs):
            calls.append(str(audio_path))
            return original_transcribe(provider, audio_path, **kwargs)

        subtitles = output / "subtitles"
        with patch.object(FasterWhisperASRProvider, "transcribe", counted):
            recognized = self.invoke(["transcribe-video", str(SOURCE), "--language", "zh",
                "--local-model-only", "--output-dir", str(subtitles)])
            self.assertEqual(recognized["status"], "awaiting_correction")
            raw_path, alias_path = subtitles / "raw_timeline.json", subtitles / "timeline.json"
            raw_bytes, alias_bytes = raw_path.read_bytes(), alias_path.read_bytes()
            self.assertEqual(load_timeline(raw_path), prior, "ASR differs; stop before paid correction requests")
            saved_bytes = (subtitles / "corrected_timeline.json").read_bytes()
            self.invoke(["correction", "set-text", str(subtitles), "--segment-id", "2", "--text", CORRECTED_TEXT])
            inspected = self.invoke(["correction", "inspect", str(subtitles)])
            self.assertEqual(inspected["unsaved_segments"], 1)
            self.assertFalse(inspected["reviewed"])
            self.assertEqual((subtitles / "corrected_timeline.json").read_bytes(), saved_bytes)
            saved = self.invoke(["correction", "save", str(subtitles)])
            self.assertTrue(saved["reviewed"])
            self.assertEqual(saved["edited_segments"], 1)
            voice_file = output / "voices.json"
            voice_file.write_text(json.dumps(VOICES, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            before_dubbing_asr = len(calls)
            with patch("voice_dubbing.tts.elevenlabs.ElevenLabsTTSProvider", ChangedSentenceOnly):
                result = self.invoke(["dub-video", str(SOURCE), "--timeline", str(subtitles),
                    "--voices-file", str(voice_file), "--model-id", "eleven_multilingual_v2",
                    "--language", "zh", "--output-format", "mp3_44100_128", "--max-retries", "0",
                    "--cache-dir", str(cache_dir), "--output-dir", str(output / "results")])
            self.assertEqual(len(calls) - before_dubbing_asr, 0)
            self.assertEqual(len(calls), 1)

        report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(report["asr_calls"], 0)
        self.assertEqual(report["cache_hits"], 2)
        self.assertEqual(report["generated_segments"], 2)
        self.assertEqual(report["supplier_requests"], 2)
        self.assertEqual(raw_path.read_bytes(), raw_bytes)
        self.assertEqual(alias_path.read_bytes(), alias_bytes)
        self.assertEqual(PRIOR.read_bytes(), prior_bytes)
        self.assertEqual(fingerprints(ROOT / "cache" / "tts"), cache_before)
        corrected = load_saved(subtitles)
        self.assertEqual([s.edited for s in corrected.segments], [False, True])
        details = []
        for voice in report["voices"]:
            directory = Path(voice["audio_path"]).parent
            self.assertTrue(Path(voice["video_path"]).is_file())
            self.assertEqual((directory / "logs" / "decode-check.log").read_text(encoding="utf-8"), "")
            per_voice = json.loads((directory / "dubbing_report.json").read_text(encoding="utf-8"))
            self.assertFalse(per_voice["is_mock"])
            self.assertEqual([s["text"] for s in per_voice["segments"]], [prior.segments[0].text, CORRECTED_TEXT])
            self.assertEqual([s["cache_hit"] for s in per_voice["segments"]], [True, False])
            self.assertEqual([s["subtitle_edited"] for s in per_voice["segments"]], [False, True])
            self.assertEqual([(s["start_ms"], s["end_ms"]) for s in per_voice["segments"]],
                             [(s.start_ms, s.end_ms) for s in prior.segments])
            details.append({"voice": voice["voice"]["name"], "video": voice["video_path"],
                "audio_ms": voice["output_duration_ms"], "video_ms": voice["video_duration_ms"],
                "segments": per_voice["segments"], "warnings": per_voice["warnings"]})
        check = {"initial_asr_calls": len(calls), "correction_and_dubbing_asr_calls": 0,
            "raw_file_sha256_before": hashlib.sha256(raw_bytes).hexdigest(),
            "raw_file_sha256_after": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            "legacy_alias_unchanged": alias_path.read_bytes() == alias_bytes,
            "prior_timeline_unchanged": PRIOR.read_bytes() == prior_bytes,
            "production_cache_unchanged": fingerprints(ROOT / "cache" / "tts") == cache_before,
            "cache_hits": 2, "generated_segments": 2, "supplier_requests": 2,
            "subtitle_source": report["subtitle_source"], "voices": details}
        (output / "acceptance_check.json").write_text(json.dumps(check, ensure_ascii=False, indent=2) + "\n",
                                                      encoding="utf-8")
        print(json.dumps({"acceptance": str(output), "report": result["report"],
                          "cache_hits": 2, "generated_segments": 2, "asr_calls": 1,
                          "dubbing_asr_calls": 0}, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
