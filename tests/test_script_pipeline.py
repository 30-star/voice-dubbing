import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from voice_dubbing.correction.operations import create_correction
from voice_dubbing.errors import MediaError, ProviderError, ValidationError
from voice_dubbing.models import (ScriptBatchDubbingJob, ScriptDubbingSelection, SpeechRequest,
                                  TranscriptSegment, TranscriptTimeline, VoiceProfile)
from voice_dubbing.script_pipeline import run_script_batch
from voice_dubbing.scripts import copy_script, create_script, load_script_selections, save_script, set_segment_text
from voice_dubbing.timeline import save_timeline
from voice_dubbing.tts import CachedTTSProvider

from test_batch_dubbing import CountingTTS, fake_renderer


class ScriptPipelineTests(unittest.TestCase):
    def setUp(self):
        self.timeline = TranscriptTimeline((TranscriptSegment("1", 100, 500, "第一句"),
                                            TranscriptSegment("2", 600, 1000, "第二句")), 1200)
        self.base = replace(create_correction(self.timeline), reviewed=True)
        self.original = create_script(self.base, base_path=Path("corrected_timeline.json"), id="original", name="原版")
        self.revised = set_segment_text(copy_script(self.original, id="revised", name="修改版"), "2", "新第二句", base=self.base)
        self.voices = (VoiceProfile("bill", "Bill", "elevenlabs", "bill-id", "model"),
                       VoiceProfile("sarah", "Sarah", "elevenlabs", "sarah-id", "model"))

    def test_cold_and_warm_cache_cross_script_reuse_without_asr(self):
        for preload in (False, True):
            with self.subTest(preload=preload), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = root / "source.mp4"
                source.touch()
                original_path = root / "source-timeline.json"
                save_timeline(self.timeline, original_path)
                original_bytes = original_path.read_bytes()
                cache = root / "cache"
                calls = []

                class Tracking(CountingTTS):
                    def synthesize(self, request, *, output_dir):
                        calls.append((request.voice_id, request.text))
                        return super().synthesize(request, output_dir=output_dir)

                def factory(voice):
                    return CachedTTSProvider(Tracking(), cache)

                if preload:
                    for voice in self.voices:
                        provider = factory(voice)
                        for segment in self.timeline.segments:
                            provider.synthesize(SpeechRequest(segment.id, segment.text, voice.voice_id, "zh"),
                                                output_dir=root / "seed" / voice.id / segment.id)
                    calls.clear()
                job = ScriptBatchDubbingJob(source, self.base, (
                    ScriptDubbingSelection(self.original, self.voices),
                    ScriptDubbingSelection(self.revised, self.voices),
                ))
                with patch("voice_dubbing.asr.FasterWhisperASRProvider.transcribe", side_effect=AssertionError("ASR forbidden")):
                    result = run_script_batch(job, output_dir=root / "results", provider_factory=factory,
                                              language="zh", renderer=fake_renderer)
                self.assertEqual(result.status, "succeeded")
                self.assertEqual(len(result.combinations), 4)
                self.assertEqual(len(calls), 2 if preload else 6)
                if preload:
                    self.assertEqual(calls, [("bill-id", "新第二句"), ("sarah-id", "新第二句")])
                report = json.loads(result.report_path.read_text(encoding="utf-8"))
                self.assertEqual(report["asr_calls"], 0)
                self.assertEqual(report["cache_hits"], 6 if preload else 2)
                self.assertEqual(report["generated_segments"], 2 if preload else 6)
                self.assertEqual(report["supplier_requests"], 2 if preload else 6)
                self.assertEqual(original_path.read_bytes(), original_bytes)
                self.assertEqual((root / "results" / "timeline.json").read_bytes(), original_bytes)
                self.assertEqual(len({item.result.video_path.parent for item in result.combinations}), 4)
                for item in result.combinations:
                    self.assertTrue(item.result.video_path.is_file())
                    audio_report = json.loads((item.result.audio_path.parent / "dubbing_report.json").read_text(encoding="utf-8"))
                    self.assertEqual([(s["start_ms"], s["end_ms"]) for s in audio_report["segments"]],
                                     [(100, 500), (600, 1000)])
                    if item.script.id == "revised":
                        self.assertEqual([s["cache_hit"] for s in audio_report["segments"]], [True, False])
                self.assertFalse(any((root / "results" / "variants").glob("*/timeline.json")))

    def test_failed_combination_does_not_stop_later_voices(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            source.touch()
            class Failing(CountingTTS):
                def synthesize(self, request, *, output_dir):
                    if request.voice_id == "bill-id" and request.text == "新第二句":
                        raise ProviderError("injected changed-sentence failure")
                    return super().synthesize(request, output_dir=output_dir)
            job = ScriptBatchDubbingJob(source, self.base, (
                ScriptDubbingSelection(self.original, self.voices),
                ScriptDubbingSelection(self.revised, self.voices),
            ))
            result = run_script_batch(job, output_dir=root / "out",
                                      provider_factory=lambda _: CachedTTSProvider(Failing(), root / "cache"),
                                      renderer=fake_renderer)
            self.assertEqual(result.status, "partial_success")
            self.assertEqual([item.result.status for item in result.combinations],
                             ["succeeded", "succeeded", "failed", "succeeded"])
            self.assertEqual(result.combinations[2].result.segment_cache[-1]["status"], "failed")

    def test_invalid_binding_and_duplicate_variants_rejected(self):
        bad = replace(self.revised, text_overrides={"3": "unknown"})
        with self.assertRaisesRegex(ValidationError, "unknown"):
            ScriptBatchDubbingJob(Path("source.mp4"), self.base,
                                 (ScriptDubbingSelection(bad, self.voices),))
        with self.assertRaisesRegex(ValidationError, "duplicate script"):
            ScriptBatchDubbingJob(Path("source.mp4"), self.base,
                                 (ScriptDubbingSelection(self.original, self.voices),) * 2)

    def test_renderer_failure_and_all_failed_combinations_are_isolated(self):
        for all_fail in (False, True):
            with self.subTest(all_fail=all_fail), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = root / "source.mp4"
                source.touch()
                attempted = []

                def renderer(video, audio, target, **kwargs):
                    attempted.append(target.parent.name)
                    if all_fail or target.parent.name == "bill":
                        raise MediaError("injected renderer failure")
                    return fake_renderer(video, audio, target, **kwargs)

                job = ScriptBatchDubbingJob(source, self.base, (
                    ScriptDubbingSelection(self.original, self.voices),
                    ScriptDubbingSelection(self.revised, self.voices),
                ))
                result = run_script_batch(job, output_dir=root / "out",
                    provider_factory=lambda _: CountingTTS(), renderer=renderer)
                self.assertEqual(attempted, ["bill", "sarah", "bill", "sarah"])
                self.assertEqual(result.status, "failed" if all_fail else "partial_success")
                self.assertTrue(all(item.result.audio_path.is_file() for item in result.combinations))
                report = json.loads(result.report_path.read_text(encoding="utf-8"))
                self.assertEqual(report["completed_combinations"], 4)
                self.assertEqual(report["status"], result.status)

    def test_manifest_relative_paths_and_per_variant_voice_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            save_script(self.original, root / "scripts" / "original.json")
            save_script(self.revised, root / "scripts" / "revised.json")
            voice_dict = {"id": "bill", "name": "Bill", "provider": "elevenlabs",
                          "voice_id": "bill-id", "model_id": "model"}
            sarah = {**voice_dict, "id": "sarah", "name": "Sarah", "voice_id": "sarah-id"}
            path = root / "jobs.json"
            path.write_text(json.dumps([
                {"script": "scripts/original.json", "voices": [voice_dict]},
                {"script": "scripts/revised.json", "voices": [voice_dict, sarah]},
            ]), encoding="utf-8")
            selections = load_script_selections(path)
            self.assertEqual([item.script.id for item in selections], ["original", "revised"])
            self.assertEqual([len(item.voices) for item in selections], [1, 2])
            source = root / "source.mp4"
            source.touch()
            job = ScriptBatchDubbingJob(source, self.base, selections)
            output = root / "out"
            output.mkdir()
            (output / "keep.txt").touch()
            with self.assertRaisesRegex(MediaError, "must be empty"):
                run_script_batch(job, output_dir=output, provider_factory=lambda _: CountingTTS())
            result = run_script_batch(job, output_dir=root / "fresh",
                provider_factory=lambda _: CountingTTS(), renderer=fake_renderer)
            self.assertEqual([(item.script.id, item.result.voice.id) for item in result.combinations],
                             [("original", "bill"), ("revised", "bill"), ("revised", "sarah")])
