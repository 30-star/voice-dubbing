"""Duration matching of measured audio, independent of ASR and TTS providers."""

from .matcher import DURATION_TOLERANCE_MS, MatchedSpeech, match_speech

__all__ = ["DURATION_TOLERANCE_MS", "MatchedSpeech", "match_speech"]
