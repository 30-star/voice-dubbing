"""Errors shared by the standalone package and future providers."""


class VoiceDubbingError(Exception):
    """Base error for the voice dubbing module."""


class ValidationError(VoiceDubbingError, ValueError):
    """Invalid public model or timeline input."""


class ProviderError(VoiceDubbingError):
    """An ASR or TTS provider failed to complete its operation."""


class MediaError(VoiceDubbingError):
    """A source video could not be probed or converted to audio."""
