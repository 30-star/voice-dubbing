import json
import tempfile
import unittest
from pathlib import Path

from voice_dubbing.errors import ValidationError
from voice_dubbing.models import TranscriptSegment, TranscriptTimeline
from voice_dubbing.timeline import load_timeline, save_timeline, timeline_from_dict, timeline_to_dict


class TimelineSerializationTests(unittest.TestCase):
    def test_chinese_round_trip_preserves_text_and_shape(self):
        original = TranscriptTimeline([TranscriptSegment("一", 0, 1250, "你好，世界\n第二行")], 3000)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "测试 timeline.json"
            save_timeline(original, path)
            raw = path.read_text(encoding="utf-8")
            self.assertIn("你好，世界", raw)
            self.assertNotIn("\\u4f60", raw)
            self.assertEqual(load_timeline(path), original)
            self.assertEqual(list(json.loads(raw)), ["segments", "duration_ms"])

    def test_bad_json_or_missing_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.json"
            path.write_text("{bad", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "cannot read timeline JSON"):
                load_timeline(path)
        for value in [
            {},
            {"segments": []},
            {"segments": "not a list", "duration_ms": 0},
            {"segments": [{"id": "1", "start_ms": 0}], "duration_ms": 500},
        ]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                timeline_from_dict(value)

    def test_round_trip_dictionary(self):
        timeline = TranscriptTimeline([], 0)
        self.assertEqual(timeline_from_dict(timeline_to_dict(timeline)), timeline)


if __name__ == "__main__":
    unittest.main()
