import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from voice_dubbing.dubbing_pipeline import synthesize_timeline
from voice_dubbing.errors import ProviderError, ValidationError
from voice_dubbing.models import SpeechRequest, TranscriptSegment, TranscriptTimeline
from voice_dubbing.tts import CachedTTSProvider
from voice_dubbing.tts.concurrent import IsolatedTTSProvider, ordered_synthesis, validate_concurrency
from test_batch_dubbing import CountingTTS


class ConcurrentTests(unittest.TestCase):
    def test_bounded_out_of_order_work_keeps_subtitle_order(self):
        lock = threading.Lock()
        active = peak = 0
        completion = []

        def work(index):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(active, peak)
            time.sleep(0.08 if index == 0 else 0.01)
            with lock:
                active -= 1
                completion.append(index)
            return index

        self.assertEqual(list(ordered_synthesis(range(12), work, 4)), list(range(12)))
        self.assertEqual(peak, 4)
        self.assertNotEqual(completion[0], 0)

    def test_failure_stops_dispatch_but_waits_for_inflight(self):
        calls = []
        finished = []
        barrier = threading.Barrier(4)

        def work(index):
            calls.append(index)
            barrier.wait(timeout=2)
            if index == 0:
                raise ProviderError("injected failure")
            time.sleep(0.03)
            finished.append(index)
            return index

        with self.assertRaisesRegex(ProviderError, "injected"):
            list(ordered_synthesis(range(20), work, 4))
        self.assertEqual(sorted(calls), [0, 1, 2, 3])
        self.assertEqual(sorted(finished), [1, 2, 3])

    def test_serial_and_invalid_limits(self):
        self.assertEqual(list(ordered_synthesis(range(4), lambda x: x, 1)), list(range(4)))
        for value in (0, 9, True, 2.5):
            with self.assertRaises(ValidationError):
                validate_concurrency(value)

    def test_cache_single_flight_and_exact_isolated_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            instances = []

            def factory():
                adapter = CountingTTS()
                original = adapter.synthesize
                def slow(request, *, output_dir):
                    time.sleep(0.03)
                    return original(request, output_dir=output_dir)
                adapter.synthesize = slow
                instances.append(adapter)
                return CachedTTSProvider(adapter, root / "cache")

            provider = IsolatedTTSProvider(factory(), factory)
            def work(index):
                return provider.synthesize(SpeechRequest(str(index), "重复一句", "bill", "zh"),
                    output_dir=root / str(index))
            results = list(ordered_synthesis(range(8), work, 4))
            self.assertEqual(sum(p.calls for p in instances), 1)
            self.assertEqual(provider.cache_hits, 7)
            self.assertEqual(provider.provider_calls, 1)
            self.assertEqual(provider.supplier_requests, 1)
            self.assertEqual(len(provider.cache_events), 8)
            self.assertEqual(sum(e["generated"] for e in provider.cache_events), 1)
            for index, result in enumerate(results):
                self.assertEqual(result.segment_id, str(index))
                self.assertEqual(result.audio_path.parent, root / str(index))
                self.assertTrue((root / str(index) / "speech.mp3").is_file())

    def test_parallel_pipeline_keeps_times_wav_and_cache_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def factory():
                return CachedTTSProvider(CountingTTS(), root / "cache")
            timeline = TranscriptTimeline(tuple(TranscriptSegment(str(i), i * 500, (i+1)*500,
                                              f"字幕{i}") for i in range(8)), 4000)
            for run in ("cold", "warm"):
                provider = IsolatedTTSProvider(factory(), factory)
                result = synthesize_timeline(timeline, provider=provider, voice_id="bill",
                    output_dir=root / run, tts_concurrency=4)
                self.assertEqual([s.id for s in result.segments], [str(i) for i in range(8)])
                self.assertEqual([s.start_ms for s in result.segments], [i*500 for i in range(8)])
                self.assertEqual(result.output_duration_ms, 4000)
                self.assertEqual(provider.supplier_requests, 8 if run == "cold" else 0)
                report = json.loads((root / run / "dubbing_report.json").read_text(encoding="utf-8"))
                self.assertEqual(report["tts_concurrency"], 4)
                self.assertEqual(sum(bool(s["cache_hit"]) for s in report["segments"]),
                                 0 if run == "cold" else 8)

    def test_failed_pipeline_keeps_completed_audio_and_diagnostics(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def factory():
                adapter = CountingTTS()
                original = adapter.synthesize
                def call(request, *, output_dir):
                    if request.segment_id == "0":
                        time.sleep(0.04)
                        raise ProviderError("failed first sentence")
                    return original(request, output_dir=output_dir)
                adapter.synthesize = call
                return CachedTTSProvider(adapter, root / "cache")
            provider = IsolatedTTSProvider(factory(), factory)
            timeline = TranscriptTimeline(tuple(TranscriptSegment(str(i), i*500, (i+1)*500,
                                              f"字幕{i}") for i in range(12)), 6000)
            with self.assertRaises(ProviderError):
                synthesize_timeline(timeline, provider=provider, voice_id="bill",
                    output_dir=root / "failed", tts_concurrency=4)
            report = json.loads((root / "failed/dubbing_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["failed_segment_id"], "0")
            self.assertEqual(len(report["synthesis_events"]), 4)
            self.assertTrue((root / "failed/segments/000002/speech.wav").is_file())
            self.assertFalse((root / "failed/dubbed_audio.wav").exists())

    def test_identical_failed_requests_are_not_repeated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            adapters = []
            def factory():
                adapter = CountingTTS(fail=True)
                adapters.append(adapter)
                return CachedTTSProvider(adapter, root / "cache")
            provider = IsolatedTTSProvider(factory(), factory)
            barrier = threading.Barrier(4)
            def work(index):
                barrier.wait(timeout=2)
                try:
                    provider.synthesize(SpeechRequest(str(index), "失败同一句", "bill"),
                                        output_dir=root / str(index))
                except ProviderError:
                    return index
            self.assertEqual(list(ordered_synthesis(range(4), work, 4)), list(range(4)))
            self.assertEqual(sum(p.calls for p in adapters), 1)
            self.assertEqual(provider.supplier_requests, 1)
            self.assertEqual(len(provider.cache_events), 4)

    def test_cli_defaults_and_range(self):
        from voice_dubbing.cli import _parser
        parser = _parser()
        args = parser.parse_args(["dub-timeline", "corrected_timeline.json", "--provider", "fake"])
        self.assertEqual(args.tts_concurrency, 4)
        args = parser.parse_args(["dub-timeline", "corrected_timeline.json", "--provider", "fake", "--tts-concurrency", "8"])
        self.assertEqual(args.tts_concurrency, 8)


if __name__ == "__main__":
    unittest.main()
