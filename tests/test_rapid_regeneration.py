import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from voice_dubbing.audio import dubbing
from voice_dubbing.audio.dubbing import normalize_speech, wav_frames
from voice_dubbing.batch_pipeline import run_batch
from voice_dubbing.correction import initialize_correction, edit_correction, save_correction, resolve_tts_timeline
from voice_dubbing.errors import MediaError
from voice_dubbing.models import BatchDubbingJob, TranscriptSegment, TranscriptTimeline, VoiceProfile
from voice_dubbing.tts import CachedTTSProvider
from test_batch_dubbing import CountingTTS, write_source


class RapidRegenerationTests(unittest.TestCase):
    def test_verified_standard_wav_is_reused_without_converter_and_without_changing_samples(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root / "source.wav", root / "normalized.wav"
            with wave.open(str(source), "wb") as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(24000)
                audio.writeframes(struct.pack("<hhhh", -32768, -1500, 1200, 32767) * 100)
            with patch.object(dubbing, "_executable", return_value="unused"), patch.object(dubbing, "_run_ffmpeg") as ffmpeg:
                frames = normalize_speech(source, target, root / "normalize.log", ffmpeg_path=None)
            ffmpeg.assert_not_called()
            self.assertEqual(frames, 400)
            self.assertEqual(source.read_bytes(), target.read_bytes())
            self.assertIn("no conversion", (root / "normalize.log").read_text())

    def test_truncated_and_empty_wav_cannot_enter_copy_path(self):
        for empty in (False, True):
            with self.subTest(empty=empty), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); source = root / "source.wav"
                with wave.open(str(source), "wb") as audio:
                    audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(24000)
                    audio.writeframes(b"" if empty else b"\x01\x00" * 100)
                if not empty: source.write_bytes(source.read_bytes()[:-2])
                with patch.object(dubbing, "_executable", return_value="unused"), patch.object(
                    dubbing, "_run_ffmpeg", side_effect=MediaError("decoder required")
                ) as ffmpeg, self.assertRaisesRegex(MediaError, "decoder required"):
                    normalize_speech(source, root / "output.wav", root / "log.txt", ffmpeg_path=None)
                ffmpeg.assert_called_once()
                self.assertFalse((root / "output.wav").exists())

    def test_nonstandard_wav_still_uses_ffmpeg_conversion(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / "stereo.wav"
            with wave.open(str(source), "wb") as audio:
                audio.setnchannels(2); audio.setsampwidth(2); audio.setframerate(44100)
                audio.writeframes(struct.pack("<hh", 4000, 2000) * 4410)
            with patch.object(dubbing, "_run_ffmpeg", wraps=dubbing._run_ffmpeg) as ffmpeg:
                frames = normalize_speech(source, root / "output.wav", root / "log.txt", ffmpeg_path=None)
            ffmpeg.assert_called_once()
            self.assertEqual(frames, 2400)
            self.assertEqual(wav_frames(root / "output.wav"), 2400)

    def test_consecutive_saved_sentence_edits_only_generate_new_text_and_keep_prior_videos(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / "中文 视频.mp4"
            write_source(source, seconds=0.9)
            raw = TranscriptTimeline([
                TranscriptSegment("1", 0, 300, "第一句"),
                TranscriptSegment("2", 300, 600, "第二句"),
                TranscriptSegment("3", 600, 900, "第三句"),
            ], 900)
            subtitles = root / "subtitles"
            initialize_correction(raw, subtitles); save_correction(subtitles)
            raw_bytes = (subtitles / "raw_timeline.json").read_bytes()
            provider = CountingTTS()
            voice = VoiceProfile("bill", "Bill", "elevenlabs", "bill-id", "model")
            clips = []

            def generate(name):
                timeline, origin = resolve_tts_timeline(subtitles)
                with patch("voice_dubbing.batch_pipeline.transcribe_video", side_effect=AssertionError("No repeat ASR")) as asr:
                    result = run_batch(BatchDubbingJob(source, timeline, (voice,)), output_dir=root / name,
                        provider_factory=lambda _: CachedTTSProvider(provider, root / "cache"), language="zh", subtitle_source=origin)
                asr.assert_not_called()
                self.assertEqual(result.asr_calls, 0)
                self.assertEqual(result.status, "succeeded")
                report = json.loads((result.voices[0].audio_path.parent / "dubbing_report.json").read_text(encoding="utf-8"))
                self.assertEqual([(r["id"], r["start_ms"], r["end_ms"]) for r in report["segments"]],
                    [(s.id, s.start_ms, s.end_ms) for s in raw.segments])
                clips.append((result.voices[0].video_path, result.voices[0].video_path.read_bytes()))
                return result.voices[0], report

            generate("initial")
            self.assertEqual(provider.calls, 3)
            edit_correction(subtitles, segment_id="1", text="第一句已修改"); save_correction(subtitles)
            first, report = generate("first-edit")
            self.assertEqual((provider.calls, first.cache_hits, first.generated_segments), (4, 2, 1))
            self.assertEqual([r["cache_hit"] for r in report["segments"]], [False, True, True])
            edit_correction(subtitles, segment_id="2", text="第二句也修改"); save_correction(subtitles)
            second, report = generate("second-edit")
            self.assertEqual((provider.calls, second.cache_hits, second.generated_segments), (5, 2, 1))
            self.assertEqual([r["cache_hit"] for r in report["segments"]], [True, False, True])
            self.assertEqual([r["text"] for r in report["segments"]], ["第一句已修改", "第二句也修改", "第三句"])
            repeated, _ = generate("unchanged")
            self.assertEqual((provider.calls, repeated.cache_hits, repeated.generated_segments), (5, 3, 0))
            self.assertTrue(all(path.read_bytes() == content for path, content in clips))
            self.assertEqual(raw_bytes, (subtitles / "raw_timeline.json").read_bytes())
