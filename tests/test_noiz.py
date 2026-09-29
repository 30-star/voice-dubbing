import contextlib
import io
import json
import os
import socket
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from voice_dubbing.cli import main
from voice_dubbing.errors import MediaError, ProviderError, ValidationError
from voice_dubbing.models import SpeechRequest, TranscriptSegment, TranscriptTimeline
from voice_dubbing.tts import CachedTTSProvider, NoizTTSProvider, default_registry
from voice_dubbing.dubbing_pipeline import synthesize_timeline
from test_elevenlabs import Response, failure, pcm


def wav_bytes(duration_ms=400, rate=24000):
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(rate)
        wav.writeframes(pcm(duration_ms, rate))
    return out.getvalue()


def voice_page(rows, total=None):
    return Response(json.dumps({"code": 200, "data": {"total_count": len(rows) if total is None else total,
                                                      "voices": rows}}, ensure_ascii=False).encode())


class NoizTests(unittest.TestCase):
    def test_missing_key_invalid_config_and_foreign_model_are_explicit(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ProviderError, "NOIZ_API_KEY"):
                NoizTTSProvider()
        for kwargs in ({"model_id": "eleven_multilingual_v2"}, {"output_format": "pcm_24000"},
                       {"parameters": {"quality_preset": True}}, {"parameters": {"stream": True}},
                       {"parameters": {"trim_silence": "false"}}, {"parameters": {"emo": {"Joy": 2}}},
                       {"parameters": {"duration": -1}}, {"timeout": 0}, {"speed": float("nan")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                NoizTTSProvider(api_key="private-key", **kwargs)

    def test_official_multipart_parameters_raw_audio_and_measured_duration(self):
        provider = NoizTTSProvider(api_key="private-key", speed=1.2,
                                 parameters={"quality_preset": 4, "emo": {"Joy": .6}})
        raw = wav_bytes()
        with tempfile.TemporaryDirectory() as folder, patch("voice_dubbing.tts.noiz.urlopen", return_value=Response(raw)) as opened:
            out = Path(folder)
            speech = provider.synthesize(SpeechRequest("a", "中文测试", "noiz-own-voice", "zh"), output_dir=out)
            request = opened.call_args.args[0]
            self.assertEqual(request.full_url, "https://noiz.ai/v1/text-to-speech")
            self.assertEqual(request.get_header("Authorization"), "private-key")
            self.assertTrue(request.get_header("Content-type").startswith("multipart/form-data; boundary="))
            body = request.data.decode()
            for name, value in (("voice_id", "noiz-own-voice"), ("target_lang", "zh"), ("text", "中文测试"),
                                ("quality_preset", "4"), ("speed", "1.2"), ("stream", "false")):
                self.assertIn(f'name="{name}"\r\n\r\n{value}\r\n', body)
            self.assertNotIn("model_id", body)
            self.assertEqual((out / "speech.original.wav").read_bytes(), raw)
            self.assertEqual(speech.duration_ms, 400)
            self.assertEqual(speech.segment_id, "a")
            self.assertEqual(provider.synthesis_http_requests, 1)
            self.assertNotIn("private-key", (out / "noiz.log").read_text())

    def test_mp3_response_is_preserved_and_decode_receives_original(self):
        provider = NoizTTSProvider(api_key="key", output_format="mp3")
        with tempfile.TemporaryDirectory() as folder, patch("voice_dubbing.tts.noiz.urlopen", return_value=Response(b"mp3-test-binary")), \
                patch("voice_dubbing.tts.noiz.normalize_speech", return_value=24000) as decoded:
            out = Path(folder)
            speech = provider.synthesize(SpeechRequest("1", "文字", "voice"), output_dir=out)
            self.assertEqual((out / "speech.original.mp3").read_bytes(), b"mp3-test-binary")
            self.assertEqual(decoded.call_args.args[0], out / "speech.original.mp3")
            self.assertEqual(speech.duration_ms, 1000)

    def test_temporary_http_retry_honors_retry_after_and_counts_attempts(self):
        provider = NoizTTSProvider(api_key="key", max_retries=2)
        clock = [1000.0]
        def sleep(seconds):
            clock[0] += seconds
        with tempfile.TemporaryDirectory() as folder, patch("voice_dubbing.tts.noiz.time.monotonic", side_effect=lambda: clock[0]), \
            patch("voice_dubbing.tts.noiz.time.sleep", side_effect=sleep) as slept, patch(
            "voice_dubbing.tts.noiz.urlopen", side_effect=[failure(429, headers={"Retry-After": "120"}),
                                                        failure(503), Response(wav_bytes())]) as opened:
            provider.synthesize(SpeechRequest("1", "文字", "voice"), output_dir=Path(folder))
            self.assertEqual(opened.call_count, 3)
            self.assertEqual(provider.synthesis_http_requests, 3)
            self.assertEqual([c.args[0] for c in slept.call_args_list], [60, 2])

    def test_http_200_json_rate_limit_retries_after_server_delay(self):
        provider = NoizTTSProvider(api_key="rate-test", max_retries=2)
        clock = [1000.0]
        def sleep(seconds):
            clock[0] += seconds
        limited = Response(b'{"message":"You\'ve hit the rate limit. Please retry in 20 seconds"}')
        with tempfile.TemporaryDirectory() as folder, \
            patch("voice_dubbing.tts.noiz.time.monotonic", side_effect=lambda: clock[0]), \
            patch("voice_dubbing.tts.noiz.time.sleep", side_effect=sleep) as slept, \
            patch("voice_dubbing.tts.noiz.urlopen", side_effect=[limited, Response(wav_bytes())]) as opened:
            provider.synthesize(SpeechRequest("1", "文字", "voice"), output_dir=Path(folder))
            self.assertEqual(opened.call_count, 2)
            self.assertEqual(provider.synthesis_http_requests, 2)
            self.assertEqual([c.args[0] for c in slept.call_args_list], [20])
            self.assertIn("reason=rate_limit retry_after=20", (Path(folder) / "noiz.log").read_text())
            self.assertEqual((Path(folder) / "speech.original.wav").read_bytes(), wav_bytes())

    def test_json_rate_limit_retry_is_bounded_and_does_not_cache_errors(self):
        provider = NoizTTSProvider(api_key="rate-bound-test", max_retries=2)
        clock = [1000.0]
        def sleep(seconds):
            clock[0] += seconds
        limited = Response(b'{"code":429,"message":"Rate limit exceeded","data":{"retry_after":20}}')
        with tempfile.TemporaryDirectory() as folder, \
            patch("voice_dubbing.tts.noiz.time.monotonic", side_effect=lambda: clock[0]), \
            patch("voice_dubbing.tts.noiz.time.sleep", side_effect=sleep) as slept, \
            patch("voice_dubbing.tts.noiz.urlopen", return_value=limited) as opened:
            root = Path(folder)
            cached = CachedTTSProvider(provider, root / "cache")
            with self.assertRaisesRegex(ProviderError, "Rate limit"):
                cached.synthesize(SpeechRequest("1", "文字", "voice"), output_dir=root / "out")
            self.assertEqual(opened.call_count, 3)
            self.assertEqual([c.args[0] for c in slept.call_args_list], [20, 20])
            self.assertEqual(list((root / "cache").rglob("manifest.json")), [])

    def test_account_cooldown_is_shared_between_workers_not_credentials(self):
        first = NoizTTSProvider(api_key="shared-account")
        second = NoizTTSProvider(api_key="shared-account")
        other = NoizTTSProvider(api_key="other-account")
        self.assertIs(first._rate_gate, second._rate_gate)
        self.assertIsNot(first._rate_gate, other._rate_gate)
        clock = [1000.0]
        sent_at = []
        def sleep(seconds):
            clock[0] += seconds
        def response(*args, **kwargs):
            sent_at.append(clock[0])
            return Response(wav_bytes())
        with tempfile.TemporaryDirectory() as folder, \
            patch("voice_dubbing.tts.noiz.time.monotonic", side_effect=lambda: clock[0]), \
            patch("voice_dubbing.tts.noiz.time.sleep", side_effect=sleep), \
            patch("voice_dubbing.tts.noiz.urlopen", side_effect=response):
            first._rate_gate.defer(20)
            other.synthesize(SpeechRequest("1", "文字", "voice"), output_dir=Path(folder) / "other")
            second.synthesize(SpeechRequest("2", "文字", "voice"), output_dir=Path(folder) / "shared")
        self.assertEqual(sent_at, [1000.0, 1020.0])

    def test_http_200_insufficient_credits_is_not_retried(self):
        with tempfile.TemporaryDirectory() as folder, patch("voice_dubbing.tts.noiz.urlopen", return_value=Response(
            b'{"code":429,"message":"Insufficient credits. Please add a payment method"}')) as opened, \
            patch("voice_dubbing.tts.noiz.time.sleep") as slept:
            with self.assertRaisesRegex(ProviderError, "Insufficient credits"):
                NoizTTSProvider(api_key="credits-test").synthesize(
                    SpeechRequest("1", "文字", "voice"), output_dir=Path(folder))
            self.assertEqual(opened.call_count, 1)
            slept.assert_not_called()

    def test_auth_error_is_not_retried_and_secret_is_redacted(self):
        provider = NoizTTSProvider(api_key="private-key")
        with tempfile.TemporaryDirectory() as folder, patch("voice_dubbing.tts.noiz.urlopen", side_effect=failure(
                401, b'{"message":"private-key is invalid"}')) as opened:
            with self.assertRaisesRegex(ProviderError, r"HTTP 401: \[REDACTED\]"):
                provider.synthesize(SpeechRequest("1", "文字", "voice"), output_dir=Path(folder))
            self.assertEqual(opened.call_count, 1)
            self.assertNotIn("private-key", (Path(folder) / "noiz.log").read_text())

    def test_ambiguous_timeouts_do_not_duplicate_paid_request(self):
        class Interrupted(Response):
            def read(self):
                raise socket.timeout()
        for error in (socket.timeout(), Interrupted(b"")):
            with self.subTest(error=type(error)), tempfile.TemporaryDirectory() as folder, patch(
                "voice_dubbing.tts.noiz.urlopen", side_effect=error if isinstance(error, Exception) else None,
                return_value=error) as opened:
                with self.assertRaisesRegex(ProviderError, "not retried"):
                    NoizTTSProvider(api_key="key").synthesize(SpeechRequest("1", "文字", "voice"), output_dir=Path(folder))
                self.assertEqual(opened.call_count, 1)

    def test_empty_json_and_bad_audio_fail_without_cache_entry(self):
        for body, error in ((b"", ProviderError), (b'{"message":"private-key"}', ProviderError), (b"bad wav", MediaError)):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as folder, patch(
                    "voice_dubbing.tts.noiz.urlopen", return_value=Response(body)):
                root = Path(folder)
                cached = CachedTTSProvider(NoizTTSProvider(api_key="private-key"), root / "cache")
                with self.assertRaises(error):
                    cached.synthesize(SpeechRequest("1", "文字", "voice"), output_dir=root / "out")
                self.assertEqual(list((root / "cache").rglob("manifest.json")), [])

    def test_text_limit_and_existing_output_fail_before_network(self):
        with tempfile.TemporaryDirectory() as folder, patch("voice_dubbing.tts.noiz.urlopen") as opened:
            provider = NoizTTSProvider(api_key="key")
            with self.assertRaisesRegex(ValidationError, "5000"):
                provider.synthesize(SpeechRequest("1", "字" * 5001, "voice"), output_dir=Path(folder))
            (Path(folder) / "speech.wav").write_bytes(b"keep")
            with self.assertRaisesRegex(ProviderError, "already exists"):
                provider.synthesize(SpeechRequest("1", "文字", "voice"), output_dir=Path(folder))
            opened.assert_not_called()

    def test_voice_catalog_is_paginated_provider_specific_and_read_only(self):
        page = [{"voice_id": f"voice-{n}", "display_name": f"声音{n}"} for n in range(100)]
        with patch("voice_dubbing.tts.noiz.urlopen", side_effect=[voice_page(page, 101),
            voice_page([{"voice_id": "voice-100", "display_name": "中文声音"}], 101), voice_page([])]) as opened:
            provider = NoizTTSProvider(api_key="key")
            voices = provider.list_voices()
            self.assertEqual(len(voices), 101)
            self.assertEqual(provider.synthesis_http_requests, 0)
            self.assertIn("skip=1", opened.call_args_list[1].args[0].full_url)
            self.assertIn("voice_type=custom", opened.call_args_list[2].args[0].full_url)
            self.assertTrue(all(v.provider == "noiz" and v.model_id == "noiz-v1" for v in voices))

    def test_malformed_or_repeating_catalog_is_rejected(self):
        row = [{"voice_id": "one", "display_name": "声音"}]
        for responses in ([Response(b"{}")], [voice_page([], 10)], [voice_page(row, 500), voice_page(row, 500)]):
            with patch("voice_dubbing.tts.noiz.urlopen", side_effect=responses), self.assertRaises(ProviderError):
                NoizTTSProvider(api_key="key").list_voices()

    def test_registry_cli_and_environment_are_provider_local(self):
        with patch.dict(os.environ, {"NOIZ_API_KEY": "private-key", "NOIZ_SPEED": "1.2",
                                    "ELEVENLABS_MODEL_ID": "foreign-model"}, clear=True):
            registry = default_registry()
            config = registry.configuration("noiz")
            self.assertEqual(config.model_id, "noiz-v1")
            self.assertEqual(config.speed, 1.2)
            out = io.StringIO()
            with patch("voice_dubbing.tts.noiz.urlopen", side_effect=[voice_page([
                {"voice_id": "own-voice", "display_name": "中文声音"}]), voice_page([])]), contextlib.redirect_stdout(out):
                self.assertEqual(main(["tts-voices", "--provider", "noiz"]), 0)
            self.assertEqual(json.loads(out.getvalue())["voices"][0]["voice_id"], "own-voice")
            self.assertNotIn("private-key", out.getvalue())
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(["tts-voices", "--provider", "noiz"]), 2)
            self.assertIn("NOIZ_API_KEY", err.getvalue())

    def test_cache_hits_preserve_raw_files_and_effective_parameters_separate_entries(self):
        with tempfile.TemporaryDirectory() as folder, patch("voice_dubbing.tts.noiz.urlopen", return_value=Response(wav_bytes())) as opened:
            root = Path(folder); registry = default_registry()
            with patch.dict(os.environ, {"NOIZ_API_KEY": "private-key", "PATH": os.environ.get("PATH", "")}, clear=True):
                provider = CachedTTSProvider(registry.create(registry.configuration("noiz")), root / "cache")
                request = SpeechRequest("first", "相同文字", "own-voice", "zh")
                provider.synthesize(request, output_dir=root / "one")
                provider.synthesize(SpeechRequest("second", request.text, request.voice_id, "zh"), output_dir=root / "two")
                self.assertEqual(opened.call_count, 1)
                self.assertEqual(provider.cache_hits, 1)
                self.assertEqual((root / "one/speech.original.wav").read_bytes(), (root / "two/speech.original.wav").read_bytes())
                for settings in ({"speed": 1.2}, {"parameters": {"quality_preset": 4}}, {"output_format": "mp3"}):
                    changed = CachedTTSProvider(registry.create(registry.configuration("noiz", settings={"noiz": settings})), root / "cache")
                    self.assertNotEqual(provider._entry(request), changed._entry(request))
                self.assertNotIn("private-key", "".join(p.read_text() for p in (root / "cache").rglob("*.json")))

    def test_real_adapter_contract_works_with_existing_mix_and_duration_matcher(self):
        timeline = TranscriptTimeline([TranscriptSegment("1", 0, 1000, "中文一句"),
                                       TranscriptSegment("2", 1000, 2000, "中文二句")], 2000)
        with tempfile.TemporaryDirectory() as folder, patch("voice_dubbing.tts.noiz.urlopen",
            side_effect=[Response(wav_bytes(1800)), Response(wav_bytes(500))]) as opened:
            result = synthesize_timeline(timeline, provider=NoizTTSProvider(api_key="key"),
                                        voice_id="noiz-voice", language="zh", output_dir=Path(folder) / "out")
            self.assertEqual(opened.call_count, 2)
            self.assertFalse(result.is_mock)
            self.assertEqual(result.segments[0].actual_duration_ms, 1800)
            self.assertLessEqual(result.segments[0].playback_duration_ms, 1000)
            self.assertEqual(result.output_duration_ms, 2000)
