import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from voice_dubbing.batch_pipeline import dub_video, run_batch
from voice_dubbing.cli import main
from voice_dubbing.correction import (edit_correction, initialize_correction, load_saved,
                                     resolve_tts_timeline, save_correction)
from voice_dubbing.models import BatchDubbingJob, SpeechRequest, VoiceProfile
from voice_dubbing.scripts import create_script, save_script
from dataclasses import replace
from voice_dubbing.timeline import save_timeline
from voice_dubbing.tts import CachedTTSProvider
from test_batch_dubbing import CountingASR, CountingTTS, fake_renderer, write_source
from test_cli import invoke
from test_scripts import timeline


def call(arguments):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(arguments)
    return code, json.loads(stdout.getvalue()) if stdout.getvalue() else None, stderr.getvalue()


class CorrectionCLITests(unittest.TestCase):
    def test_full_cli_edit_save_discard_restore_and_init_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            legacy, review = root / "旧字幕.json", root / "校正 字幕"
            save_timeline(timeline(), legacy)
            before = legacy.read_bytes()
            self.assertEqual(invoke("correction", "init", str(legacy), "--output-dir", str(review)).returncode, 0)
            self.assertEqual(invoke("correction", "init", str(legacy), "--output-dir", str(review)).returncode, 2)
            self.assertEqual(invoke("correction", "set-text", str(review), "--segment-id", "2", "--text", "修正文字").returncode, 0)
            state = json.loads(invoke("correction", "inspect", str(review)).stdout)
            self.assertEqual(state["unsaved_segments"], 1)
            self.assertFalse(state["reviewed"])
            self.assertEqual(invoke("correction", "discard", str(review)).returncode, 0)
            for args in (("set-text", "--segment-id", "2", "--text", "修正文字"), ("save",),
                         ("restore", "--segment-id", "2"), ("save",), ("restore-all",), ("save",)):
                result = invoke("correction", args[0], str(review), *args[1:])
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(load_saved(review).to_timeline(), timeline())
            self.assertTrue(load_saved(review).reviewed)
            self.assertEqual(legacy.read_bytes(), before)

    def test_default_video_pauses_without_tts_or_key(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            write_source(source)
            asr = CountingASR()
            with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "", "ELEVENLABS_TIMEOUT": "invalid-for-TTS"}), \
                    patch("voice_dubbing.video_cli.create_asr", return_value=asr), \
                    patch("voice_dubbing.tts.elevenlabs.ElevenLabsTTSProvider") as tts:
                code, result, error = call(["dub-video", str(source), "--voices", "bill-id,sarah-id", "--no-asr-cache",
                                            "--output-dir", str(root / "review")])
            self.assertEqual(code, 0, error)
            self.assertEqual(asr.calls, 1)
            self.assertEqual(result["status"], "awaiting_correction")
            self.assertFalse(load_saved(root / "review").reviewed)
            tts.assert_not_called()
            self.assertFalse(any((root / "review").rglob("*.mp4")))

    def test_explicit_accept_asr_cli_reviews_then_renders(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            write_source(source)
            asr = CountingASR()
            def accepted(*args, **kwargs):
                kwargs["renderer"] = fake_renderer
                return dub_video(*args, **kwargs)
            with patch("voice_dubbing.video_cli.create_asr", return_value=asr), \
                    patch("voice_dubbing.tts.elevenlabs.ElevenLabsTTSProvider", return_value=CountingTTS()), \
                    patch("voice_dubbing.video_cli.dub_video", side_effect=accepted):
                code, result, error = call(["dub-video", str(source), "--voices", "bill-id", "--accept-asr", "--no-asr-cache",
                    "--no-cache", "--output-dir", str(root / "out")])
            self.assertEqual(code, 0, error)
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["asr_calls"], 1)
            self.assertEqual(asr.calls, 1)
            self.assertTrue(load_saved(root / "out").reviewed)
            report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
            self.assertTrue(report["subtitle_source"]["reviewed"])
            self.assertEqual(report["subtitle_source"]["segments"][0]["edited"], False)

    def test_unreviewed_legacy_and_bad_binding_block_all_tts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            initialize_correction(timeline(), root / "review")
            legacy = root / "old.json"
            save_timeline(timeline(), legacy)
            for source in (legacy, root / "review"):
                with self.subTest(source=source), patch("voice_dubbing.tts.fake.FakeTTSProvider") as tts:
                    code, result, error = call(["dub-timeline", str(source), "--provider", "fake"])
                    self.assertEqual(code, 2)
                    tts.assert_not_called()
            save_correction(root / "review")
            corrected = root / "review" / "corrected_timeline.json"
            data = json.loads(corrected.read_text(encoding="utf-8"))
            data["segments"][0]["start_ms"] += 1
            corrected.write_text(json.dumps(data), encoding="utf-8")
            with patch("voice_dubbing.tts.elevenlabs.ElevenLabsTTSProvider") as tts:
                video = root / "source.mp4"
                video.touch()
                code, _, _ = call(["dub-video", str(video), "--timeline", str(root / "review"),
                                    "--voices", "bill-id", "--output-dir", str(root / "out")])
                self.assertEqual(code, 2)
                tts.assert_not_called()

    def test_prepared_video_uses_saved_text_without_asr_or_extraction(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            source.touch()
            review = root / "review"
            initialize_correction(timeline(), review)
            edit_correction(review, segment_id="2", text="正确文字")
            save_correction(review)
            requests = []
            class Tracking(CountingTTS):
                def synthesize(self, request, *, output_dir):
                    requests.append((request.voice_id, request.text))
                    return super().synthesize(request, output_dir=output_dir)
            def prepared(*args, **kwargs):
                kwargs["renderer"] = fake_renderer
                return run_batch(*args, **kwargs)
            with patch("voice_dubbing.tts.elevenlabs.ElevenLabsTTSProvider", side_effect=lambda **_: Tracking()), \
                    patch("voice_dubbing.video_cli.run_batch", side_effect=prepared), \
                    patch("voice_dubbing.video_cli.create_asr", side_effect=AssertionError("ASR forbidden")), \
                    patch("voice_dubbing.pipeline.extract_audio", side_effect=AssertionError("extract forbidden")):
                code, result, error = call(["dub-video", str(source), "--timeline", str(review),
                    "--voices", "bill-id,sarah-id", "--no-cache", "--output-dir", str(root / "out")])
            self.assertEqual(code, 0, error)
            self.assertEqual(result["asr_calls"], 0)
            self.assertEqual(requests, [("bill-id", "你好你好"), ("bill-id", "正确文字"),
                                        ("sarah-id", "你好你好"), ("sarah-id", "正确文字")])
            report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
            self.assertEqual(report["subtitle_source"]["kind"], "corrected")
            details = json.loads((Path(result["voices"][0]["audio"]).parent / "dubbing_report.json").read_text(encoding="utf-8"))
            self.assertEqual([s["subtitle_edited"] for s in details["segments"]], [False, True])

    def test_changed_sentence_invalidates_only_corresponding_voice_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            source.touch()
            review = root / "review"
            initialize_correction(timeline(), review)
            profiles = (VoiceProfile("bill", "Bill", "elevenlabs", "bill-id", "model"),
                        VoiceProfile("sarah", "Sarah", "elevenlabs", "sarah-id", "model"))
            cache_dir = root / "cache"
            requests = []
            class Tracking(CountingTTS):
                def synthesize(self, request, *, output_dir):
                    requests.append((request.voice_id, request.text))
                    return super().synthesize(request, output_dir=output_dir)
            factory = lambda _: CachedTTSProvider(Tracking(), cache_dir)
            for voice in profiles:
                cache = factory(voice)
                for segment in timeline().segments:
                    cache.synthesize(SpeechRequest(segment.id, segment.text, voice.voice_id, "zh"),
                                     output_dir=root / "seed" / voice.id / segment.id)
            requests.clear()
            edit_correction(review, segment_id="2", text="正确文字")
            save_correction(review)
            corrected, metadata = resolve_tts_timeline(review)
            result = run_batch(BatchDubbingJob(source, corrected, profiles), output_dir=root / "out",
                provider_factory=factory, language="zh", renderer=fake_renderer, subtitle_source=metadata)
            report = json.loads(result.report_path.read_text(encoding="utf-8"))
            self.assertEqual(requests, [("bill-id", "正确文字"), ("sarah-id", "正确文字")])
            self.assertEqual(report["cache_hits"], 2)
            self.assertEqual(report["generated_segments"], 2)
            self.assertEqual(report["asr_calls"], 0)

    def test_variants_with_different_raw_identity_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            review = root / "review"
            initialize_correction(timeline(), review)
            base = save_correction(review)
            script = create_script(base, base_path=review / "corrected_timeline.json", id="old", name="Old")
            script = replace(script, base_timeline=replace(script.base_timeline, raw_timeline_sha256="0" * 64))
            save_script(script, root / "old.json")
            edit_correction(review, segment_id="2", text="正确文字")
            save_correction(review)
            (root / "jobs.json").write_text(json.dumps([{"script": "old.json", "voices": [{
                "id": "bill", "name": "Bill", "provider": "elevenlabs", "voice_id": "bill-id", "model_id": "model"}]}]),
                encoding="utf-8")
            before = (root / "old.json").read_bytes()
            with patch("voice_dubbing.tts.elevenlabs.ElevenLabsTTSProvider") as provider:
                code, _, error = call(["dub-scripts", str(root / "source.mp4"), "--timeline", str(review),
                                        "--variants-file", str(root / "jobs.json")])
            self.assertEqual(code, 2)
            self.assertIn("fingerprint", error)
            provider.assert_not_called()
            self.assertEqual((root / "old.json").read_bytes(), before)
