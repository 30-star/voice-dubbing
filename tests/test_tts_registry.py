import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from voice_dubbing.cli import main
from voice_dubbing.config.tts import TTSConfiguration, load_provider_settings
from voice_dubbing.errors import ProviderError, ValidationError
from voice_dubbing.models import SpeechRequest
from voice_dubbing.tts import CachedTTSProvider, ProviderDescriptor, ProviderRegistry, default_registry
from test_batch_dubbing import CountingTTS


class RegistryTests(unittest.TestCase):
    def test_discovery_is_offline_and_never_returns_secrets(self):
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "private-registry-key"}, clear=True), patch(
                "voice_dubbing.tts.elevenlabs.urlopen", side_effect=AssertionError("network forbidden")):
            registry = default_registry()
            listing = registry.list()
            self.assertEqual([v["id"] for v in listing], ["elevenlabs", "noiz", "volcengine", "minimax", "siliconflow"])
            self.assertTrue(listing[0]["implemented"] and listing[0]["configured"])
            self.assertTrue(listing[1]["implemented"] and not listing[1]["configured"])
            self.assertTrue(listing[1]["supports_voice_listing"])
            self.assertTrue(all(not v["implemented"] for v in listing[2:]))
            self.assertNotIn("private-registry-key", json.dumps(listing))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["tts-providers"]), 0)
            self.assertNotIn("private-registry-key", out.getvalue())

    def test_missing_or_unimplemented_provider_never_falls_back(self):
        with patch.dict(os.environ, {}, clear=True):
            registry = default_registry()
            self.assertFalse(registry.list()[0]["configured"])
            with self.assertRaisesRegex(ProviderError, "not configured"):
                registry.create(registry.configuration("elevenlabs"))
            for name in ("volcengine", "minimax", "siliconflow"):
                with self.assertRaisesRegex(ProviderError, "not implemented"):
                    registry.create(registry.configuration(name))
            with self.assertRaises(ValidationError):
                registry.descriptor("missing")

    def test_provider_local_configuration_precedence_and_no_foreign_env(self):
        with patch.dict(os.environ, {"ELEVENLABS_MODEL_ID": "eleven-env", "MINIMAX_MODEL_ID": "mini-env",
                                    "ELEVENLABS_TIMEOUT": "41"}, clear=True):
            registry = default_registry()
            config = registry.configuration("elevenlabs", model_id="voice-model",
                options={"timeout": 12}, settings={"elevenlabs": {"model_id": "file-model", "timeout": 20}})
            self.assertEqual((config.model_id, config.timeout), ("voice-model", 12))
            self.assertEqual(registry.configuration("minimax").model_id, "mini-env")
            self.assertEqual(registry.configuration("minimax").timeout, 60)
            self.assertEqual(registry.configuration("elevenlabs").model_id, "eleven-env")
            self.assertEqual(registry.configuration("elevenlabs", settings={"elevenlabs": {"model_id": "file-model"}}).model_id, "file-model")

    def test_configuration_file_rejects_duplicate_keys_and_nested_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "providers.json"
            path.write_text('{"providers":{"elevenlabs":{"speed":1,"speed":2}}}')
            with self.assertRaises(ValidationError):
                load_provider_settings(path)
            path.write_text('{"providers":{"elevenlabs":{"api_key":"not-allowed"}}}')
            with self.assertRaises(ValidationError):
                load_provider_settings(path)
        with self.assertRaises(ValidationError):
            TTSConfiguration("test", parameters={"nested": {"token": "not-allowed"}})
        for value in (0, float("nan"), True):
            with self.assertRaises(ValidationError):
                TTSConfiguration("test", speed=value)

    def test_new_adapter_requires_only_registration_and_existing_contract(self):
        registry = ProviderRegistry()
        provider = CountingTTS()
        provider.provider_id = "test-other"
        registry.register(ProviderDescriptor("test-other", "Test", "TEST", "model", "pcm_24000"), lambda config: provider)
        configured = registry.create(TTSConfiguration("test-other", speed=1.2))
        self.assertIs(configured.provider, provider)
        self.assertEqual(configured.cache_parameters, {"speed": 1.2})
        with self.assertRaises(ValidationError):
            registry.register(ProviderDescriptor("test-other", "Duplicate", "TEST"), None)

    def test_unsupported_elevenlabs_speed_is_not_silently_ignored(self):
        with self.assertRaisesRegex(ValidationError, "default speed"):
            default_registry().create(TTSConfiguration("elevenlabs", speed=1.1))

    def test_registry_preserves_read_only_adapter_contract_and_rejects_foreign_factory(self):
        class ReadOnlyAdapter:
            __slots__ = ()
            provider_id = "readonly"
            def synthesize(self, request, *, output_dir):
                return CountingTTS().synthesize(request, output_dir=output_dir)
        registry = ProviderRegistry()
        registry.register(ProviderDescriptor("readonly", "Read only", "READONLY"), lambda config: ReadOnlyAdapter())
        with tempfile.TemporaryDirectory() as folder:
            configured = registry.create(TTSConfiguration("readonly", model_id="selected-model", output_format="pcm_24000"))
            speech = configured.synthesize(SpeechRequest("1", "文字", "voice"), output_dir=Path(folder))
            self.assertEqual(speech.segment_id, "1")
            self.assertEqual(configured.cache_parameters, {"speed": 1.0})
            self.assertEqual((configured.model_id, configured.output_format), ("selected-model", "pcm_24000"))
        registry.register(ProviderDescriptor("foreign", "Foreign", "FOREIGN"), lambda config: ReadOnlyAdapter())
        with self.assertRaises(ProviderError):
            registry.create(TTSConfiguration("foreign"))


class MultiProviderCacheTests(unittest.TestCase):
    def test_provider_model_voice_speed_and_audio_parameters_isolate_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            request = SpeechRequest("1", "相同文字", "same-voice", "zh")
            provider = CountingTTS()
            baseline = CachedTTSProvider(provider, root / "cache")
            baseline.synthesize(request, output_dir=root / "initial")
            for i, (service, model, voice, speed, settings) in enumerate([
                ("different-provider", "model", "same-voice", 1, {}),
                ("elevenlabs", "different-model", "same-voice", 1, {}),
                ("elevenlabs", "model", "different-voice", 1, {}),
                ("elevenlabs", "model", "same-voice", 1.2, {}),
                ("elevenlabs", "model", "same-voice", 1, {"pitch": 0.2}),
            ]):
                provider.provider_id, provider.model_id = service, model
                cache = CachedTTSProvider(provider, root / "cache", settings={"speed": speed, **settings})
                self.assertFalse(cache.is_cached(SpeechRequest("2", request.text, voice, "zh")))
            provider.provider_id, provider.model_id = baseline.provider_id, baseline.model_id
            hit = CachedTTSProvider(provider, root / "cache", settings={"speed": 1.0})
            hit.synthesize(SpeechRequest("new-id", request.text, request.voice_id, "zh"), output_dir=root / "hit")
            self.assertEqual(hit.cache_hits, 1)
            self.assertEqual(provider.calls, 1)

    def test_valid_legacy_elevenlabs_cache_is_reused_without_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            provider = CountingTTS()
            provider.provider_id = "elevenlabs"
            request = SpeechRequest("1", "旧缓存文字", "bill", "zh")
            cache = CachedTTSProvider(provider, root / "cache")
            original = provider.synthesize(request, output_dir=root / "fixture")
            legacy = cache._entry(request, legacy=True)
            cache._publish(legacy, original, root / "fixture")
            manifest_path = legacy / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["version"] = 1
            manifest_path.write_text(json.dumps(manifest))
            original_bytes = manifest_path.read_bytes()
            provider.calls = 0
            provider.cache_parameters = {"speed": 1.0}
            upgraded = CachedTTSProvider(provider, root / "cache")
            self.assertTrue(upgraded.is_cached(request))
            upgraded.synthesize(request, output_dir=root / "new-task")
            self.assertEqual((upgraded.cache_hits, provider.calls), (1, 0))
            self.assertEqual(manifest_path.read_bytes(), original_bytes)
            faster = CachedTTSProvider(provider, root / "cache", settings={"speed": 1.2})
            self.assertFalse(faster.is_cached(request))

    def test_nested_secret_rejection_and_parameter_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = CountingTTS()
            with self.assertRaisesRegex(ValidationError, "non-secret"):
                CachedTTSProvider(provider, Path(folder), settings={"nested": {"api_key": "private"}})
            params = {"voice_settings": {"stability": 0.3}}
            cache = CachedTTSProvider(provider, Path(folder), settings=params)
            request = SpeechRequest("1", "你好", "voice")
            before = cache._entry(request)
            params["voice_settings"]["stability"] = 0.7
            self.assertEqual(cache._entry(request), before)


if __name__ == "__main__":
    unittest.main()
