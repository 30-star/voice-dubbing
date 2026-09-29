import csv
import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from voice_dubbing.audio.dubbing import SAMPLE_RATE, wav_frames
from voice_dubbing.dubbing_pipeline import synthesize_timeline
from voice_dubbing.errors import MediaError, ProviderError, ValidationError
from voice_dubbing.models import SpeechAudio, TranscriptSegment, TranscriptTimeline
from voice_dubbing.tts import FakeTTSProvider


def sample_at(path: Path, millisecond: int) -> int:
    with wave.open(str(path), "rb") as audio:
        audio.setpos(millisecond * 24)
        return struct.unpack("<h", audio.readframes(1))[0]


class ConstantProvider:
    provider_id = "constant-test"
    is_mock = True

    def __init__(self, durations: dict[str, int], *, fail_id: str | None = None,
                 missing_audio: bool = False):
        self.durations = durations
        self.fail_id = fail_id
        self.missing_audio = missing_audio
        self.calls: list[tuple[str, str]] = []

    def synthesize(self, request, *, output_dir):
        self.calls.append((request.segment_id, request.voice_id))
        if request.segment_id == self.fail_id:
            raise ProviderError("synthetic provider failure")
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "speech.wav"
        if not self.missing_audio:
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(SAMPLE_RATE)
                audio.writeframes(struct.pack("<h", 4000) * self.durations[request.segment_id] * 24)
        # Deliberately wrong metadata: the pipeline must measure the WAV itself.
        return SpeechAudio(request.segment_id, path, 999, SAMPLE_RATE, 1)


class AudioDubbingTests(unittest.TestCase):
    def test_fake_provider_has_measured_sentence_audio_and_reports(self):
        timeline = TranscriptTimeline([
            TranscriptSegment("a", 0, 500, "你好"),
            TranscriptSegment("b", 500, 1000, "世界"),
        ], 1000)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "dub"
            result = synthesize_timeline(
                timeline, provider=FakeTTSProvider(), voice_id="fake-default", output_dir=output
            )
            self.assertEqual(result.output_duration_ms, 1000)
            self.assertTrue(result.is_mock)
            self.assertEqual([segment.audio_duration_ms for segment in result.segments], [500, 500])
            self.assertTrue((output / "segments" / "000001" / "speech.wav").is_file())
            self.assertTrue((output / "segments" / "000002" / "speech.wav").is_file())
            self.assertEqual(wav_frames(result.audio_path), 24_000)
            report = json.loads((output / "dubbing_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "succeeded")
            self.assertTrue(report["is_mock"])
            with (output / "dubbing_report.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["target_duration_ms"] for row in rows], ["500", "500"])
            self.assertEqual([row["overflow_ms"] for row in rows], ["0", "0"])
            self.assertEqual([row["voice_id"] for row in rows], ["fake-default", "fake-default"])

    def test_silence_positions_and_measured_duration(self):
        timeline = TranscriptTimeline([
            TranscriptSegment("a", 100, 200, "first"),
            TranscriptSegment("b", 400, 500, "second"),
        ], 600)
        provider = ConstantProvider({"a": 100, "b": 100})
        with tempfile.TemporaryDirectory() as folder:
            result = synthesize_timeline(
                timeline, provider=provider, voice_id="one", output_dir=Path(folder) / "dub"
            )
            self.assertEqual(provider.calls, [("a", "one"), ("b", "one")])
            self.assertEqual([segment.audio_duration_ms for segment in result.segments], [100, 100])
            self.assertEqual(wav_frames(result.audio_path), 600 * 24)
            self.assertEqual(sample_at(result.audio_path, 50), 0)
            self.assertGreater(sample_at(result.audio_path, 150), 2000)
            self.assertEqual(sample_at(result.audio_path, 300), 0)
            self.assertGreater(sample_at(result.audio_path, 450), 2000)
            self.assertEqual(sample_at(result.audio_path, 550), 0)

    def test_overlap_and_tail_are_preserved(self):
        timeline = TranscriptTimeline([
            TranscriptSegment("a", 0, 200, "first"),
            TranscriptSegment("b", 200, 400, "second"),
        ], 400)
        with tempfile.TemporaryDirectory() as folder:
            result = synthesize_timeline(
                timeline, provider=ConstantProvider({"a": 400, "b": 300}),
                voice_id="one", output_dir=Path(folder) / "dub", match_duration=False
            )
            self.assertEqual(result.output_duration_ms, 500)
            self.assertEqual(wav_frames(result.audio_path), 500 * 24)
            self.assertEqual([segment.overrun_ms for segment in result.segments], [200, 100])
            self.assertEqual([segment.overflow_ms for segment in result.segments], [200, 100])
            self.assertEqual(result.segments[0].overlap_with_next_ms, 200)
            self.assertGreater(sample_at(result.audio_path, 300), sample_at(result.audio_path, 100) * 1.5)
            self.assertGreater(sample_at(result.audio_path, 450), 2000)
            self.assertTrue(any("exceeds subtitle end by 200 ms" in warning for warning in result.warnings))
            self.assertTrue(any("overlaps next start by 200 ms" in warning for warning in result.warnings))
            self.assertFalse(any("extends timeline" in warning for warning in result.warnings))

    def test_116_ms_tail_is_accepted_without_warning(self):
        timeline = TranscriptTimeline([TranscriptSegment("a", 0, 2160, "first")], 2160)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "dub"
            result = synthesize_timeline(
                timeline, provider=ConstantProvider({"a": 2276}), voice_id="one", output_dir=output
            )
            self.assertEqual(result.output_duration_ms, 2276)
            self.assertEqual(result.segments[0].overflow_ms, 116)
            self.assertEqual(result.warnings, ())
            report = json.loads((output / "dubbing_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["duration_tolerance_ms"], 150)
            self.assertEqual(report["warnings"], [])

    def test_negative_overflow_is_preserved(self):
        timeline = TranscriptTimeline([TranscriptSegment("a", 0, 500, "first")], 500)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "dub"
            result = synthesize_timeline(
                timeline, provider=ConstantProvider({"a": 100}), voice_id="one", output_dir=output
            )
            self.assertEqual(result.segments[0].overflow_ms, -400)
            report = json.loads((output / "dubbing_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["segments"][0]["overflow_ms"], -400)
            self.assertEqual(report["segments"][0]["actual_duration_ms"], 100)
            with (output / "dubbing_report.csv").open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(next(csv.DictReader(handle))["overflow_ms"], "-400")

    def test_empty_timeline_and_zero_length_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "empty"
            result = synthesize_timeline(
                TranscriptTimeline([], 500), provider=FakeTTSProvider(),
                voice_id="one", output_dir=output,
            )
            self.assertEqual(wav_frames(result.audio_path), 500 * 24)
            self.assertEqual(sample_at(result.audio_path, 200), 0)
            self.assertTrue((output / "logs" / "mix.log").is_file())
            with self.assertRaises(ValidationError):
                synthesize_timeline(
                    TranscriptTimeline([], 0), provider=FakeTTSProvider(),
                    voice_id="one", output_dir=Path(folder) / "zero",
                )

    def test_provider_failure_keeps_partial_report_without_final_audio(self):
        timeline = TranscriptTimeline([
            TranscriptSegment("a", 0, 100, "first"),
            TranscriptSegment("b", 100, 200, "second"),
        ], 200)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "failed"
            with self.assertRaisesRegex(ProviderError, "synthetic provider failure"):
                synthesize_timeline(
                    timeline, provider=ConstantProvider({"a": 100}, fail_id="b"),
                    voice_id="one", output_dir=output,
                )
            self.assertFalse((output / "dubbed_audio.wav").exists())
            self.assertTrue((output / "segments" / "000001" / "speech.wav").exists())
            report = json.loads((output / "dubbing_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["failed_segment_id"], "b")
            self.assertEqual(len(report["segments"]), 1)

    def test_missing_provider_audio_and_ffmpeg_failure_are_reported(self):
        timeline = TranscriptTimeline([TranscriptSegment("a", 0, 100, "text")], 100)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "missing-audio"
            with self.assertRaisesRegex(MediaError, "produced no audio"):
                synthesize_timeline(
                    timeline, provider=ConstantProvider({"a": 100}, missing_audio=True),
                    voice_id="one", output_dir=output,
                )
            self.assertEqual(json.loads((output / "dubbing_report.json").read_text(encoding="utf-8"))["status"], "failed")
            output = Path(folder) / "missing-ffmpeg"
            with self.assertRaisesRegex(MediaError, "ffmpeg executable not found"):
                synthesize_timeline(
                    timeline, provider=ConstantProvider({"a": 100}), voice_id="one",
                    output_dir=output, ffmpeg_path=Path(folder) / "not-present.exe",
                )
            self.assertTrue((output / "dubbing_report.json").is_file())
            self.assertIn("ffmpeg executable not found",
                          (output / "logs" / "normalize-000001.log").read_text(encoding="utf-8"))

    def test_existing_output_is_protected(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            previous = output / "dubbed_audio.wav"
            previous.write_bytes(b"keep")
            with self.assertRaisesRegex(MediaError, "must be empty"):
                synthesize_timeline(
                    TranscriptTimeline([], 100), provider=FakeTTSProvider(),
                    voice_id="one", output_dir=output,
                )
            self.assertEqual(previous.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
