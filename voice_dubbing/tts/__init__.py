"""Text-to-speech provider contracts."""

from .base import TTSProvider
from .registry import ProviderDescriptor, ProviderRegistry, default_registry
from .cache import CachedTTSProvider
from .fake import FakeTTSProvider
from .elevenlabs import ElevenLabsTTSProvider
from .noiz import NoizTTSProvider

__all__ = ["TTSProvider", "CachedTTSProvider", "FakeTTSProvider", "ElevenLabsTTSProvider",
           "NoizTTSProvider", "ProviderDescriptor", "ProviderRegistry", "default_registry"]
