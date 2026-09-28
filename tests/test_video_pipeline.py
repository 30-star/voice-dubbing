import tempfile
import unittest
import json
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from voice_dubbing.audio.extraction import probe_video_duration_ms
from voice_dubbing.errors import MediaError
from voice_dubbing.models import TranscriptSegment, TranscriptTimeline
from voice_dubbing.pipeline import transcribe_video
from voice_dubbing.timeline import load_timeline


class StubASR:
    provider_id = "stub"

    def transcribe(self, audio_path, *, language=None):
        assert audio_path.is_file()
        assert language == "zh"
        return TranscriptTimeline([TranscriptSegment("1", 100, 900, "你好")], 1000)


class VideoPipelineTests(unittest.TestCase):
    def test_no_audio_track_is_reported_before_extraction(self):
        metadata = {"format": {"duration": "1.25"}, "streams": [{"codec_type": "video"}]}
        completed = CompletedProcess([], 0, json.dumps(metadata), "")
        with patch("voice_dubbing.audio.extraction._executable", return_value="ffprobe"), \
             patch("voice_dubbing.audio.extraction._run", return_value=completed):
            with self.assertRaisesRegex(MediaError, "no audio track"):
                probe_video_duration_ms(Path("silent.mp4"))

    def test_video_produces_timeline_in_separate_output_dir(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "video.mp4"
            video.touch()
            output = root / "results" / "first"

            def make_audio(_video, audio_path, *, ffmpeg_path=None):
                audio_path.write_bytes(b"test wav")

            with patch("voice_dubbing.pipeline.probe_video_duration_ms", return_value=3000), \
                 patch("voice_dubbing.pipeline.extract_audio", side_effect=make_audio):
                timeline = transcribe_video(video, provider=StubASR(), output_dir=output,
                                            language="zh")
            self.assertEqual(timeline.duration_ms, 3000)
            self.assertEqual(load_timeline(output / "timeline.json"), timeline)
            self.assertFalse((root / "timeline.json").exists())
            with self.assertRaisesRegex(MediaError, "timeline already exists"):
                transcribe_video(video, provider=StubASR(), output_dir=output, language="zh")

    def test_missing_video_and_source_directory_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaisesRegex(MediaError, "video file not found"):
                transcribe_video(root / "missing.mp4", provider=StubASR(), output_dir=root)
            video = root / "video.mp4"
            video.touch()
            with self.assertRaisesRegex(MediaError, "must differ"):
                transcribe_video(video, provider=StubASR(), output_dir=root)


if __name__ == "__main__":
    unittest.main()
