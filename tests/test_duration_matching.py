import json
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from voice_dubbing.audio.dubbing import wav_frames
from voice_dubbing.duration.matcher import match_speech, tempo_filter
from voice_dubbing.dubbing_pipeline import synthesize_timeline
from voice_dubbing.errors import MediaError, ValidationError
from voice_dubbing.models import TranscriptSegment, TranscriptTimeline
from voice_dubbing.tts import CachedTTSProvider
from test_audio_dubbing import ConstantProvider, sample_at


def tone(path, duration_ms, frequency=440):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"".join(struct.pack("<h", round(8000 * math.sin(2 * math.pi * frequency * n / 24000)))
                                for n in range(duration_ms * 24)))


class DurationMatchingTests(unittest.TestCase):
    def test_150_ms_boundary_is_untouched_without_ffmpeg(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "speech.wav"
            tone(source, 1150)
            before = source.read_bytes()
            with patch("voice_dubbing.duration.matcher._run_ffmpeg", side_effect=AssertionError("unneeded conversion")):
                result = match_speech(source, Path(folder) / "matched.wav", 1000, Path(folder) / "match.log")
            self.assertEqual(result.path, source)
            self.assertEqual(result.speed_factor, 1)
            self.assertEqual(source.read_bytes(), before)

    def test_151_ms_is_fitted_and_preserves_pitch_and_original(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "speech.wav"
            tone(source, 1151)
            original = source.read_bytes()
            result = match_speech(source, Path(folder) / "matched.wav", 1000, Path(folder) / "match.log")
            self.assertLessEqual(result.frames, 24000)
            self.assertGreater(result.frames, 22000)
            self.assertGreater(result.speed_factor, 1)
            self.assertEqual(source.read_bytes(), original)
            # Pitch measured by zero crossings stays near 440Hz, rather than
            # increasing with the playback speed like a resampling shortcut.
            with wave.open(str(result.path), "rb") as wav:
                data = struct.unpack("<" + "h" * wav.getnframes(), wav.readframes(wav.getnframes()))
            crossings = sum(a <= 0 < b for a, b in zip(data, data[1:]))
            self.assertAlmostEqual(crossings * 24000 / len(data), 440, delta=6)

    def test_large_ratio_uses_chained_factors(self):
        factors = [float(piece.split("=")[1]) for piece in tempo_filter(5).split(",")]
        self.assertTrue(all(1 <= value <= 2 for value in factors))
        self.assertEqual(math.prod(factors), 5)
        with self.assertRaises(ValidationError):
            tempo_filter(float("inf"))

    def test_complete_tail_marker_survives_speedup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "speech.wav"
            # Last 300ms has a different pitch, representing a final spoken word.
            with wave.open(str(source), "wb") as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
                wav.writeframes(b"".join(struct.pack("<h", round(8000 * math.sin(
                    2 * math.pi * (440 if n < 24000 else 880) * n / 24000)))
                    for n in range(31200)))
            result = match_speech(source, root / "matched.wav", 1000, root / "match.log")
            with wave.open(str(result.path), "rb") as wav:
                wav.setpos(round(result.frames * .85))
                count = round(result.frames * .08)
                samples = struct.unpack("<" + "h" * count, wav.readframes(count))
            crossings = sum(a <= 0 < b for a, b in zip(samples, samples[1:]))
            self.assertAlmostEqual(crossings * 24000 / count, 880, delta=20)

    def test_rounding_retry_uses_original_and_checks_measured_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "speech.wav"
            tone(source, 1800)
            commands = []

            def render(args, log):
                commands.append(args)
                tone(Path(args[-1]), 1100 if len(commands) == 1 else 990)

            with patch("voice_dubbing.duration.matcher._run_ffmpeg", side_effect=render):
                result = match_speech(source, root / "matched.wav", 1000, root / "match.log")
            self.assertEqual(len(commands), 2)
            self.assertTrue(all(args[args.index("-i") + 1] == str(source) for args in commands))
            self.assertTrue(all("atrim" not in args[args.index("-af") + 1] for args in commands))
            self.assertEqual(result.frames, 990 * 24)

    def test_pipeline_keeps_starts_raw_measurements_and_removes_large_overlap(self):
        timeline = TranscriptTimeline([TranscriptSegment("a", 100, 1100, "first"),
                                       TranscriptSegment("b", 1100, 2100, "last")], 2100)
        with tempfile.TemporaryDirectory() as folder:
            provider = ConstantProvider({"a": 1800, "b": 1300})
            out = Path(folder) / "result"
            result = synthesize_timeline(timeline, provider=provider, voice_id="one", output_dir=out)
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual([s.start_ms for s in result.segments], [100, 1100])
            self.assertEqual([s.actual_duration_ms for s in result.segments], [1800, 1300])
            self.assertEqual([s.overflow_ms for s in result.segments], [800, 300])
            self.assertEqual(result.output_duration_ms, 2100)
            self.assertEqual(result.segments[0].playback_overlap_with_next_ms, 0)
            self.assertEqual(sample_at(result.audio_path, 50), 0)
            self.assertGreater(sample_at(result.audio_path, 150), 2000)
            self.assertGreater(sample_at(result.audio_path, 1150), 2000)
            self.assertLessEqual(result.segments[0].playback_duration_ms, 1000)
            report = json.loads((out / "dubbing_report.json").read_text(encoding="utf-8"))
            self.assertTrue(report["duration_matching"])
            self.assertTrue(report["segments"][0]["duration_adjusted"])

    def test_next_start_limits_window_even_for_overlapping_subtitles(self):
        timeline = TranscriptTimeline([TranscriptSegment("a", 0, 2000, "first"),
                                       TranscriptSegment("b", 1000, 2000, "last")], 2000)
        with tempfile.TemporaryDirectory() as folder:
            result = synthesize_timeline(timeline, provider=ConstantProvider({"a": 1700, "b": 500}),
                                         voice_id="one", output_dir=Path(folder) / "out")
            self.assertEqual(result.segments[0].overflow_ms, -300)
            self.assertLessEqual(result.segments[0].playback_duration_ms, 1000)
            self.assertEqual(result.segments[0].playback_overlap_with_next_ms, 0)

    def test_failed_processing_never_publishes_final_audio(self):
        timeline = TranscriptTimeline([TranscriptSegment("a", 0, 1000, "first")], 1000)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "out"
            with patch("voice_dubbing.duration.matcher._run_ffmpeg", side_effect=MediaError("local match failed")):
                with self.assertRaisesRegex(MediaError, "local match failed"):
                    synthesize_timeline(timeline, provider=ConstantProvider({"a": 2000}),
                                        voice_id="one", output_dir=output)
            self.assertFalse((output / "dubbed_audio.wav").exists())
            self.assertTrue((output / "segments/000001/speech.wav").exists())
            self.assertEqual(json.loads((output / "dubbing_report.json").read_text())["failed_segment_id"], "a")

    def test_duplicate_start_rejected_before_provider_calls(self):
        timeline = TranscriptTimeline([TranscriptSegment("a", 0, 1000, "first"),
                                       TranscriptSegment("b", 0, 1000, "last")], 1000)
        provider = ConstantProvider({"a": 1000, "b": 1000})
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValidationError, "simultaneous"):
                synthesize_timeline(timeline, provider=provider, voice_id="one", output_dir=Path(folder) / "out")
        self.assertEqual(provider.calls, [])

    def test_cached_original_is_reused_for_different_windows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            provider = ConstantProvider({"a": 1800})
            cached = CachedTTSProvider(provider, root / "cache")
            short = synthesize_timeline(TranscriptTimeline([TranscriptSegment("a", 0, 1000, "same")], 1000),
                                        provider=cached, voice_id="one", output_dir=root / "short")
            snapshot = {p.relative_to(root / "cache"): p.read_bytes()
                        for p in (root / "cache").rglob("*") if p.is_file()}
            long = synthesize_timeline(TranscriptTimeline([TranscriptSegment("a", 0, 2000, "same")], 2000),
                                       provider=cached, voice_id="one", output_dir=root / "long")
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(cached.cache_hits, 1)
            self.assertGreater(short.segments[0].speed_factor, 1)
            self.assertEqual(long.segments[0].speed_factor, 1)
            self.assertEqual(long.segments[0].actual_duration_ms, 1800)
            self.assertEqual(snapshot, {p.relative_to(root / "cache"): p.read_bytes()
                                       for p in (root / "cache").rglob("*") if p.is_file()})
