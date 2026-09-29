"""Provider-neutral, non-secret configuration."""

from .tts import TTSConfiguration, load_provider_settings

__all__ = ["TTSConfiguration", "load_provider_settings"]
