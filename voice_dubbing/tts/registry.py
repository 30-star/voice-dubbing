"""Provider-neutral creation, discovery and configuration boundary."""

import os
from dataclasses import dataclass
from typing import Callable

from ..config.tts import TTSConfiguration, resolve_configuration
from ..errors import ProviderError, ValidationError
from .base import TTSProvider


@dataclass(frozen=True)
class ProviderDescriptor:
    id: str
    name: str
    env_prefix: str
    default_model: str | None = None
    default_format: str | None = None
    internal: bool = False
    supports_voice_listing: bool = False


class ConfiguredTTSProvider:
    """Attach cache configuration without imposing writable attributes on adapters."""
    def __init__(self, provider: TTSProvider, config: TTSConfiguration):
        self.provider = provider
        self.model_id = getattr(provider, "model_id", None) or config.model_id
        self.output_format = getattr(provider, "output_format", None) or config.output_format
        self.cache_parameters = {**getattr(provider, "cache_parameters", {}), **config.cache_parameters}

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def synthesize(self, request, *, output_dir):
        return self.provider.synthesize(request, output_dir=output_dir)


class ProviderRegistry:
    def __init__(self):
        self._entries: dict[str, tuple[ProviderDescriptor, Callable | None]] = {}

    def register(self, descriptor: ProviderDescriptor, factory: Callable[[TTSConfiguration], TTSProvider] | None):
        if not descriptor.id or descriptor.id in self._entries:
            raise ValidationError("duplicate or empty TTS provider registration")
        self._entries[descriptor.id] = descriptor, factory

    def descriptor(self, provider_id: str) -> ProviderDescriptor:
        if provider_id not in self._entries:
            raise ValidationError(f"unknown TTS provider: {provider_id}")
        return self._entries[provider_id][0]

    def configuration(self, provider_id: str, *, model_id=None, options=None, settings=None):
        return resolve_configuration(self.descriptor(provider_id), model_id=model_id,
            options=options, settings=(settings or {}).get(provider_id, {}), environ=os.environ)

    def create(self, config: TTSConfiguration) -> TTSProvider:
        self.descriptor(config.provider)
        factory = self._entries[config.provider][1]
        if factory is None:
            raise ProviderError(f"{config.provider} TTS Adapter is not implemented; no fallback was used")
        provider = factory(config)
        if getattr(provider, "provider_id", None) != config.provider or not callable(getattr(provider, "synthesize", None)):
            raise ProviderError("TTS factory returned a different provider")
        # Output-affecting parameters travel with the adapter into the cache wrapper.
        return ConfiguredTTSProvider(provider, config)

    def list(self, *, include_internal=False):
        return [dict(id=d.id, name=d.name, implemented=factory is not None,
                     configured=d.internal or bool(os.environ.get(f"{d.env_prefix}_API_KEY", "").strip()),
                     credential_env=f"{d.env_prefix}_API_KEY")
                     | {"supports_voice_listing": d.supports_voice_listing}
                for d, factory in self._entries.values() if include_internal or not d.internal]


def default_registry() -> ProviderRegistry:
    # Only this composition root knows concrete adapters; orchestration stays neutral.
    from .elevenlabs import create_from_configuration as elevenlabs_factory
    from .fake import FakeTTSProvider
    from .noiz import create_from_configuration as noiz_factory
    registry = ProviderRegistry()
    registry.register(ProviderDescriptor("elevenlabs", "ElevenLabs", "ELEVENLABS",
                      "eleven_multilingual_v2", "mp3_44100_128"), elevenlabs_factory)
    registry.register(ProviderDescriptor("noiz", "Noiz", "NOIZ", "noiz-v1", "wav",
                                         supports_voice_listing=True), noiz_factory)

    def fake_factory(config):
        if config.speed != 1 or config.parameters:
            raise ValidationError("Fake TTS does not support audio parameter overrides")
        return FakeTTSProvider()

    registry.register(ProviderDescriptor("fake", "Fake (test only)", "FAKE", internal=True), fake_factory)
    for provider, name, prefix in (("volcengine", "火山引擎", "VOLCENGINE"),
                                    ("minimax", "MiniMax", "MINIMAX"),
                                    ("siliconflow", "SiliconFlow", "SILICONFLOW")):
        registry.register(ProviderDescriptor(provider, name, prefix), None)
    return registry
