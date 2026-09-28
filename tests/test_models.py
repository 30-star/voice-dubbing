import unittest
from pathlib import Path

from voice_dubbing.errors import ValidationError
from voice_dubbing.models import (
    DubbingResult,
    DubbingTask,
    DubbingVariantResult,
    SpeechAudio,
    SpeechRequest,
    TranscriptSegment,
    TranscriptTimeline,
    VoiceSelection,
)


class TranscriptModelTests(unittest.TestCase):
    def segment(self, ident="a", start=0, end=1000, text="你好"):
        return TranscriptSegment(ident, start, end, text)

    def test_valid_timeline_accepts_gaps_and_overlaps(self):
        timeline = TranscriptTimeline(
            [self.segment("a", 100, 500), self.segment("b", 400, 800),
             self.segment("c", 1000, 1200)],
            3000,
        )
        self.assertEqual(len(timeline.segments), 3)
        self.assertIsInstance(timeline.segments, tuple)

    def test_empty_timeline_is_valid(self):
        self.assertEqual(TranscriptTimeline([], 0).segments, ())

    def test_bad_segment_times_or_text(self):
        for values in [
            ("a", -1, 100, "hello"), ("a", 100, 100, "hello"),
            ("a", 0, True, "hello"), ("a", 0, 100, "  "),
            ("", 0, 100, "hello"),
        ]:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                TranscriptSegment(*values)

    def test_bad_timeline_times_order_and_duplicate_ids(self):
        for segments, duration in [
            ([self.segment()], -1),
            ([self.segment()], 999),
            ([self.segment("a"), self.segment("a", 1000, 1500)], 2000),
            ([self.segment("a", 1000, 1500), self.segment("b", 0, 100)], 2000),
        ]:
            with self.subTest(segments=segments, duration=duration):
                with self.assertRaises(ValidationError):
                    TranscriptTimeline(segments, duration)


class DubbingModelTests(unittest.TestCase):
    def test_multi_voice_task_and_results(self):
        first = VoiceSelection("provider-a", "voice-1")
        second = VoiceSelection("provider-b", "voice-2")
        task = DubbingTask("task-1", Path("video.mp4"), Path("output"), [first, second])
        self.assertEqual(task.voices, (first, second))
        result = DubbingResult("task-1", None, [
            DubbingVariantResult(first, "succeeded", video_path=Path("one.mp4")),
            DubbingVariantResult(second, "failed", error="provider unavailable"),
        ])
        self.assertEqual([variant.status for variant in result.variants], ["succeeded", "failed"])

    def test_invalid_variant_status_combinations(self):
        voice = VoiceSelection("provider-a", "voice-1")
        for kwargs in [
            dict(status="succeeded"),
            dict(status="failed"),
            dict(status="failed", error="failed", video_path=Path("video.mp4")),
            dict(status="cancelled", video_path=Path("video.mp4")),
            dict(status="unknown"),
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                DubbingVariantResult(voice, **kwargs)

    def test_duplicate_voice_is_rejected(self):
        voice = VoiceSelection("provider-a", "voice-1")
        with self.assertRaises(ValidationError):
            DubbingTask("task", Path("video.mp4"), Path("out"), [voice, voice])

    def test_speech_contracts(self):
        request = SpeechRequest("1", "你好", "voice-1", "zh")
        audio = SpeechAudio(request.segment_id, Path("generated.wav"), 500, 24000, 1)
        self.assertEqual(audio.sample_rate_hz, 24000)
        with self.assertRaises(ValidationError):
            SpeechAudio("1", Path("bad.wav"), 0, 24000, 1)


if __name__ == "__main__":
    unittest.main()
