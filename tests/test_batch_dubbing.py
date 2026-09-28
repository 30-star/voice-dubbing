import json
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from voice_dubbing.audio.extraction import _executable
from voice_dubbing.batch_pipeline import dub_video, run_batch
from voice_dubbing.errors import MediaError, ProviderError, ValidationError
from voice_dubbing.models import (BatchDubbingJob, SpeechAudio, TranscriptSegment,
                                  TranscriptTimeline, VoiceProfile)
from voice_dubbing.renderer import RenderedVideo, render_dubbed_video
from voice_dubbing.tts import CachedTTSProvider


def write_source(path: Path, seconds: float = 0.5) -> None:
    ffmpeg = _executable("ffmpeg", None)
    result = subprocess.run([
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=blue:s=160x90:r=30:d={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=24000:duration={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
    ], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr)


def write_speech(path: Path, duration_ms: int = 300) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(struct.pack("<h", 4000) * duration_ms * 24)


class CountingASR:
    provider_id = "counting"

    def __init__(self):
        self.calls = 0

    def transcribe(self, audio_path, *, language=None):
        self.calls += 1
        assert audio_path.is_file()
        return TranscriptTimeline([TranscriptSegment("one", 0, 200, "你好")], 500)


class CountingTTS:
    provider_id = "elevenlabs"
    is_mock = True
    model_id = "model"
    output_format = "wav"

    def __init__(self, *, fail=False):
        self.calls = 0
        self.fail = fail

    def synthesize(self, request, *, output_dir):
        self.calls += 1
        if self.fail:
            raise ProviderError("injected TTS failure")
        output_dir.mkdir(parents=True, exist_ok=True)
        wav = output_dir / "speech.wav"
        write_speech(wav)
        (output_dir / "speech.mp3").write_bytes(b"raw audio fixture")
        return SpeechAudio(request.segment_id, wav, 300, 24000, 1)


def fake_renderer(video, audio, output, **kwargs):
    output.write_bytes(b"injected renderer output")
    return RenderedVideo(output, 500, 500, 160, 90)


class BatchDubbingTests(unittest.TestCase):
    def setUp(self):
        try:
            _executable("ffmpeg", None)
            _executable("ffprobe", None)
        except MediaError as exc:
            self.skipTest(str(exc))

    @staticmethod
    def profiles():
        return (VoiceProfile("bill", "Bill", "elevenlabs", "bill-id", "model"),
                VoiceProfile("sarah", "Sarah", "elevenlabs", "sarah-id", "model"))

    def test_one_asr_two_voices_and_reuse_without_asr_or_tts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "中文 source.mp4"
            write_source(source)
            asr = CountingASR()
            providers = {profile.id: CountingTTS() for profile in self.profiles()}
            cache = root / "cache"

            def factory(profile):
                return CachedTTSProvider(providers[profile.id], cache)

            result = dub_video(source, voices=self.profiles(), asr_provider=asr,
                               provider_factory=factory, output_dir=root / "first",
                                renderer=fake_renderer, accept_asr=True)
            self.assertEqual(asr.calls, 1)
            self.assertEqual(result.asr_calls, 1)
            self.assertEqual(result.status, "succeeded")
            self.assertEqual([providers[name].calls for name in ("bill", "sarah")], [1, 1])
            self.assertEqual(len(list((root / "first").glob("timeline.json"))), 1)
            self.assertTrue((root / "first" / "source_audio.wav").is_file())
            self.assertEqual(len({item.video_path.parent for item in result.voices}), 2)
            report = json.loads(result.report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["asr_calls"], 1)
            self.assertEqual(report["status"], "succeeded")
            self.assertEqual(report["supplier_requests"], 2)
            self.assertTrue(all(item["video_path"] for item in report["voices"]))

            reuse = run_batch(BatchDubbingJob(source, result.job.timeline, self.profiles()),
                              output_dir=root / "second", provider_factory=factory,
                              renderer=fake_renderer)
            self.assertEqual(reuse.status, "succeeded")
            self.assertEqual(reuse.asr_calls, 0)
            self.assertEqual(asr.calls, 1)
            self.assertEqual([providers[name].calls for name in ("bill", "sarah")], [1, 1])
            self.assertEqual([item.cache_hits for item in reuse.voices], [1, 1])
            self.assertEqual([item.supplier_requests for item in reuse.voices], [0, 0])
            self.assertEqual(report["timeline_sha256"],
                             json.loads(reuse.report_path.read_text(encoding="utf-8"))["timeline_sha256"])
            for item in reuse.voices:
                segment_dir = item.audio_path.parent / "segments" / "000001"
                self.assertTrue((segment_dir / "speech.wav").is_file())
                self.assertEqual((segment_dir / "speech.mp3").read_bytes(), b"raw audio fixture")

    def test_failure_is_isolated_and_renderer_failure_keeps_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            write_source(source)
            asr = CountingASR()
            profiles = self.profiles() + (VoiceProfile("third", "Third", "elevenlabs", "third-id", "model"),)
            providers = {voice.id: CountingTTS(fail=voice.id == "sarah") for voice in profiles}

            def renderer(video, audio, output, **kwargs):
                if output.parent.name == "bill":
                    raise MediaError("injected renderer failure")
                return fake_renderer(video, audio, output, **kwargs)

            result = dub_video(source, voices=profiles, asr_provider=asr,
                               provider_factory=lambda voice: providers[voice.id],
                                output_dir=root / "batch", renderer=renderer, accept_asr=True)
            self.assertEqual(asr.calls, 1)
            self.assertEqual(result.status, "partial_success")
            self.assertEqual([item.status for item in result.voices], ["failed", "failed", "succeeded"])
            self.assertTrue(result.voices[0].audio_path.is_file())
            self.assertIsNone(result.voices[1].audio_path)
            self.assertTrue(result.voices[2].video_path.is_file())
            self.assertTrue((root / "batch" / "sarah" / "dubbing_report.json").is_file())
            self.assertEqual(json.loads(result.report_path.read_text(encoding="utf-8"))["status"],
                             "partial_success")

    def test_invalid_voices_and_existing_output_prevent_asr(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            source.touch()
            asr = CountingASR()
            with self.assertRaisesRegex(ValidationError, "duplicate"):
                dub_video(source, voices=self.profiles() + (self.profiles()[0],),
                          asr_provider=asr, provider_factory=lambda _: CountingTTS(),
                                output_dir=root / "out", renderer=fake_renderer, accept_asr=True)
            out = root / "out"
            out.mkdir()
            (out / "keep.txt").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(MediaError, "must be empty"):
                dub_video(source, voices=self.profiles(), asr_provider=asr,
                          provider_factory=lambda _: CountingTTS(), output_dir=out,
                          renderer=fake_renderer)
            self.assertEqual(asr.calls, 0)
            self.assertEqual((out / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_all_voices_can_fail_without_masquerading_as_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            write_source(source)
            result = dub_video(source, voices=self.profiles(), asr_provider=CountingASR(),
                               provider_factory=lambda _: CountingTTS(fail=True),
                                output_dir=root / "out", renderer=fake_renderer, accept_asr=True)
            self.assertEqual(result.status, "failed")
            self.assertTrue(all(item.error for item in result.voices))
            self.assertEqual(json.loads(result.report_path.read_text(encoding="utf-8"))["status"],
                             "failed")

    def test_unsafe_profile_id_stays_inside_batch_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            write_source(source)
            profile = VoiceProfile("../中文/CON", "Alias", "elevenlabs", "voice-id", "model")
            result = dub_video(source, voices=(profile,), asr_provider=CountingASR(),
                               provider_factory=lambda _: CountingTTS(),
                               output_dir=root / "out", renderer=fake_renderer, accept_asr=True)
            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.voices[0].video_path.parent.parent, root / "out")
            self.assertEqual(json.loads(result.report_path.read_text(encoding="utf-8"))["voices"][0]
                             ["voice"]["id"], "../中文/CON")

    def test_renderer_preserves_tail_and_replaces_original_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            write_source(source)
            audio = root / "dub.wav"
            with wave.open(str(audio), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(24000)
                output.writeframes(b"\0\0" * 12000 + struct.pack("<h", 9000) * 12000)
            rendered = render_dubbed_video(source, audio, root / "render" / "output.mp4")
            self.assertTrue(rendered.path.is_file())
            self.assertEqual((rendered.width, rendered.height), (160, 90))
            self.assertGreaterEqual(rendered.duration_ms, 990)
            decoded = root / "decoded.wav"
            subprocess.run([_executable("ffmpeg", None), "-nostdin", "-hide_banner", "-loglevel", "error",
                            "-i", str(rendered.path), "-vn", "-ac", "1", "-ar", "24000",
                            "-c:a", "pcm_s16le", str(decoded)], check=True)
            with wave.open(str(decoded), "rb") as output:
                output.setpos(24000 * 9 // 10)
                tail = output.readframes(1000)
            values = struct.unpack("<" + "h" * (len(tail) // 2), tail)
            self.assertGreater(max(abs(value) for value in values), 2000)


if __name__ == "__main__":
    unittest.main()
