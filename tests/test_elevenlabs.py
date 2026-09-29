"""Offline HTTP contract tests; no ElevenLabs request is made here."""

import io
import contextlib
import json
import socket
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from voice_dubbing.errors import MediaError, ProviderError, ValidationError
from voice_dubbing.cli import main
from voice_dubbing.dubbing_pipeline import synthesize_timeline
from voice_dubbing.models import SpeechRequest, TranscriptSegment, TranscriptTimeline
from voice_dubbing.tts import ElevenLabsTTSProvider


class Response:
    status = 200
    headers = {"request-id": "test-request"}

    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body


def failure(code: int, body: bytes = b'{"detail":{"message":"rejected"}}', headers=None) -> HTTPError:
    return HTTPError("https://api.elevenlabs.io", code, "failed", headers or {}, io.BytesIO(body))


def pcm(milliseconds: int, rate: int = 24000) -> bytes:
    return struct.pack("<h", 4000) * (rate * milliseconds // 1000)


class ElevenLabsTests(unittest.TestCase):
    def test_config_validation_and_missing_key(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ProviderError, "ELEVENLABS_API_KEY"):
                ElevenLabsTTSProvider()
        with self.assertRaises(ValidationError):
            ElevenLabsTTSProvider(api_key="test-key", output_format="ulaw_8000")
        with self.assertRaises(ValidationError):
            ElevenLabsTTSProvider(api_key="test-key", timeout=0)
        with self.assertRaises(ValidationError):
            ElevenLabsTTSProvider(api_key="test-key", max_retries=-1)

    def test_pcm_request_raw_preservation_duration_and_language(self):
        provider = ElevenLabsTTSProvider(api_key="private-test-key", output_format="pcm_24000")
        with tempfile.TemporaryDirectory() as folder, patch(
            "voice_dubbing.tts.elevenlabs.urlopen", return_value=Response(pcm(350))
        ) as opened:
            output = Path(folder)
            speech = provider.synthesize(SpeechRequest("one", "你好", "voice-1", "zh"), output_dir=output)
            request = opened.call_args.args[0]
            self.assertIn("/voice-1?output_format=pcm_24000", request.full_url)
            self.assertEqual(request.get_header("Xi-api-key"), "private-test-key")
            self.assertEqual(json.loads(request.data), {"text": "你好", "model_id": "eleven_multilingual_v2"})
            self.assertEqual((output / "speech.pcm").read_bytes(), pcm(350))
            self.assertEqual(speech.audio_path, output / "speech.wav")
            self.assertEqual((speech.duration_ms, speech.sample_rate_hz, speech.channels), (350, 24000, 1))
            self.assertNotIn("private-test-key", (output / "elevenlabs.log").read_text(encoding="utf-8"))
            with wave.open(str(speech.audio_path)) as audio:
                self.assertEqual(audio.getnframes(), 350 * 24)

    def test_other_model_can_send_language_code(self):
        provider = ElevenLabsTTSProvider(api_key="test-key", model_id="eleven_flash_v2_5", output_format="pcm_24000")
        with tempfile.TemporaryDirectory() as folder, patch(
            "voice_dubbing.tts.elevenlabs.urlopen", return_value=Response(pcm(100))
        ) as opened:
            provider.synthesize(SpeechRequest("s", "你好", "voice", "zh"), output_dir=Path(folder))
            self.assertEqual(json.loads(opened.call_args.args[0].data)["language_code"], "zh")

    def test_mp3_is_kept_and_decoded(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            wav = output / "fixture.wav"
            with wave.open(str(wav), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(24000)
                audio.writeframes(pcm(250))
            mp3 = output / "fixture.mp3"
            subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(wav), str(mp3)], check=True)
            encoded = mp3.read_bytes()
            provider = ElevenLabsTTSProvider(api_key="test-key")
            with patch("voice_dubbing.tts.elevenlabs.urlopen", return_value=Response(encoded)):
                speech = provider.synthesize(SpeechRequest("one", "你好", "voice"), output_dir=output / "segment")
            self.assertEqual((output / "segment" / "speech.mp3").read_bytes(), encoded)
            self.assertGreater(speech.duration_ms, 0)
            self.assertTrue(speech.audio_path.is_file())

    def test_retries_only_transient_status_and_redacts_key(self):
        provider = ElevenLabsTTSProvider(api_key="private-test-key", output_format="pcm_24000", max_retries=2)
        transient = failure(429, b'{"detail":{"message":"private-test-key rate limited"}}', {"Retry-After": "0", "request-id": "one"})
        with tempfile.TemporaryDirectory() as folder, patch(
            "voice_dubbing.tts.elevenlabs.urlopen", side_effect=[transient, Response(pcm(100))]
        ) as opened, patch("voice_dubbing.tts.elevenlabs.time.sleep") as sleep:
            provider.synthesize(SpeechRequest("one", "你好", "voice"), output_dir=Path(folder))
            self.assertEqual(opened.call_count, 2)
            self.assertEqual(provider.synthesis_http_requests, 2)
            sleep.assert_called_once()
            log = (Path(folder) / "elevenlabs.log").read_text(encoding="utf-8")
            self.assertIn("status=429", log)
            self.assertIn("status=200", log)
            self.assertNotIn("private-test-key", log)
        with tempfile.TemporaryDirectory() as folder, patch(
            "voice_dubbing.tts.elevenlabs.urlopen", side_effect=failure(401)
        ) as opened:
            with self.assertRaisesRegex(ProviderError, "HTTP 401"):
                provider.synthesize(SpeechRequest("one", "你好", "voice"), output_dir=Path(folder))
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(provider.synthesis_http_requests, 3)
        with tempfile.TemporaryDirectory() as folder, patch(
            "voice_dubbing.tts.elevenlabs.urlopen", side_effect=[failure(503), Response(pcm(100))]
        ) as opened, patch("voice_dubbing.tts.elevenlabs.time.sleep"):
            provider.synthesize(SpeechRequest("one", "你好", "voice"), output_dir=Path(folder))
            self.assertEqual(opened.call_count, 2)
            self.assertEqual(provider.synthesis_http_requests, 5)

    def test_timeout_empty_and_corrupt_audio(self):
        provider = ElevenLabsTTSProvider(api_key="test-key", output_format="pcm_24000")
        with tempfile.TemporaryDirectory() as folder:
            for response, error in [(Response(b""), ProviderError), (Response(b"x"), MediaError)]:
                with self.subTest(response=response.body), patch("voice_dubbing.tts.elevenlabs.urlopen", return_value=response):
                    with self.assertRaises(error):
                        provider.synthesize(SpeechRequest("s", "你好", "voice"), output_dir=Path(folder))
        with tempfile.TemporaryDirectory() as folder, patch(
            "voice_dubbing.tts.elevenlabs.urlopen", side_effect=socket.timeout("private detail")
        ) as opened:
            with self.assertRaisesRegex(ProviderError, "timed out"):
                provider.synthesize(SpeechRequest("s", "你好", "voice"), output_dir=Path(folder))
            self.assertEqual(opened.call_count, 1)
        class Interrupted(Response):
            def read(self):
                raise OSError("socket closed")
        with tempfile.TemporaryDirectory() as folder, patch(
            "voice_dubbing.tts.elevenlabs.urlopen", return_value=Interrupted(b""),
        ) as opened:
            with self.assertRaisesRegex(ProviderError, "response interrupted"):
                provider.synthesize(SpeechRequest("s", "你好", "voice"), output_dir=Path(folder))
            self.assertEqual(opened.call_count, 1)

    def test_cli_settings_override_environment_and_auto_voice(self):
        from types import SimpleNamespace
        from voice_dubbing.models import TranscriptTimeline

        selected = SimpleNamespace(
            provider_id="elevenlabs", selected_voice_name="Chinese Voice",
            model_id="cli-model", output_format="pcm_24000",
            select_voice=lambda: "selected-voice",
            synthesize=lambda request, *, output_dir: None,
        )
        output = io.StringIO()
        with patch.dict("os.environ", {
            "ELEVENLABS_MODEL_ID": "env-model", "ELEVENLABS_OUTPUT_FORMAT": "mp3_44100_128",
            "ELEVENLABS_TIMEOUT": "99", "ELEVENLABS_MAX_RETRIES": "9",
            "ELEVENLABS_VOICE_ID": "env-voice",
        }), patch("voice_dubbing.cli.resolve_tts_timeline", return_value=(TranscriptTimeline([], 100), {})), patch(
            "voice_dubbing.tts.elevenlabs.ElevenLabsTTSProvider", return_value=selected
        ) as constructor, patch("voice_dubbing.cli.synthesize_timeline", return_value=SimpleNamespace(
            audio_path=Path("dubbed_audio.wav"), segments=(), output_duration_ms=100,
            provider_id="elevenlabs", voice_id="selected-voice", is_mock=False, warnings=(),
        )) as pipeline, contextlib.redirect_stdout(output):
            code = main([
                "dub-timeline", "unused.json", "--provider", "elevenlabs", "--voice", "auto",
                "--model-id", "cli-model", "--output-format", "pcm_24000", "--timeout", "30",
                "--max-retries", "1",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(constructor.call_args.kwargs["model_id"], "cli-model")
        self.assertEqual(constructor.call_args.kwargs["output_format"], "pcm_24000")
        self.assertEqual(constructor.call_args.kwargs["timeout"], 30)
        self.assertEqual(constructor.call_args.kwargs["max_retries"], 1)
        self.assertEqual(pipeline.call_args.kwargs["voice_id"], "selected-voice")
        self.assertEqual(json.loads(output.getvalue())["voice_name"], "Chinese Voice")

    def test_voice_pagination_and_chinese_priority(self):
        pages = [
            {"voices": [
                {"voice_id": "default", "name": "A", "category": "premade"},
                {"voice_id": "labeled", "name": "B", "labels": {"language": "Chinese"}},
            ], "has_more": True, "next_page_token": "next"},
            {"voices": [
                {"voice_id": "verified", "name": "C", "verified_languages": [{"language": "zh"}]},
                {"voice_id": "premade-zh", "name": "D", "category": "premade", "verified_languages": [{"language": "zh"}]},
            ], "has_more": False},
        ]
        provider = ElevenLabsTTSProvider(api_key="test-key")
        with patch("voice_dubbing.tts.elevenlabs.urlopen", side_effect=[
            Response(json.dumps(page).encode("utf-8")) for page in pages
        ]) as opened:
            self.assertEqual(provider.select_voice(), "premade-zh")
            self.assertEqual(provider.selected_voice_name, "D")
            self.assertIn("next_page_token=next", opened.call_args.args[0].full_url)

    def test_full_timeline_with_http_stub_keeps_signed_overflow(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"ELEVENLABS_API_KEY": "private-test-key"}), patch(
            "voice_dubbing.tts.elevenlabs.urlopen", side_effect=[Response(pcm(1000)), Response(pcm(3000))]
        ) as opened, contextlib.redirect_stdout(io.StringIO()):
            timeline_path = Path(folder) / "timeline.json"
            timeline_path.write_text(json.dumps({"duration_ms": 4480, "segments": [
                {"id": "1", "start_ms": 0, "end_ms": 2320, "text": "第一句"},
                {"id": "2", "start_ms": 2320, "end_ms": 4480, "text": "第二句"},
            ]}, ensure_ascii=False), encoding="utf-8")
            output = Path(folder) / "task"
            from voice_dubbing.correction import initialize_correction, save_correction
            from voice_dubbing.timeline import load_timeline
            subtitles = Path(folder) / "subtitles"
            initialize_correction(load_timeline(timeline_path), subtitles)
            save_correction(subtitles)
            code = main([
                "dub-timeline", str(subtitles), "--provider", "elevenlabs", "--voice", "one-voice",
                "--output-format", "pcm_24000", "--output-dir", str(output),
            ])
            self.assertEqual(code, 0)
            report = json.loads((output / "dubbing_report.json").read_text(encoding="utf-8"))
            self.assertNotIn("private-test-key", (output / "dubbing_report.json").read_text(encoding="utf-8"))
            self.assertEqual(opened.call_count, 2)
            self.assertFalse(report["is_mock"])
            self.assertEqual(report["timeline_duration_ms"], 4480)
            self.assertEqual(report["output_duration_ms"], 4480)
            self.assertTrue(report["segments"][1]["duration_adjusted"])
            self.assertLessEqual(report["segments"][1]["playback_duration_ms"], 2160)
            self.assertEqual([row["overflow_ms"] for row in report["segments"]], [-1320, 840])
            self.assertEqual([row["voice_id"] for row in report["segments"]], ["one-voice", "one-voice"])
            self.assertTrue((output / "segments" / "000001" / "speech.pcm").is_file())
            self.assertTrue((output / "segments" / "000002" / "speech.pcm").is_file())
            self.assertTrue((output / "dubbed_audio.wav").is_file())

    def test_real_adapter_failure_keeps_first_sentence_and_failed_report(self):
        timeline = TranscriptTimeline([
            TranscriptSegment("1", 0, 1000, "第一句"),
            TranscriptSegment("2", 1000, 2000, "第二句"),
        ], 2000)
        provider = ElevenLabsTTSProvider(api_key="test-key", output_format="pcm_24000", max_retries=0)
        with tempfile.TemporaryDirectory() as folder, patch(
            "voice_dubbing.tts.elevenlabs.urlopen", side_effect=[Response(pcm(400)), failure(401)]
        ):
            output = Path(folder) / "task"
            with self.assertRaisesRegex(ProviderError, "HTTP 401"):
                synthesize_timeline(timeline, provider=provider, voice_id="one", output_dir=output)
            report = json.loads((output / "dubbing_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["failed_segment_id"], "2")
            self.assertEqual(len(report["segments"]), 1)
            self.assertTrue((output / "segments" / "000001" / "speech.pcm").is_file())
            self.assertFalse((output / "dubbed_audio.wav").exists())


if __name__ == "__main__":
    unittest.main()
