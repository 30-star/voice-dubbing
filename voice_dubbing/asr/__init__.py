"""Speech recognition provider contracts."""

from .base import ASRProvider, VideoASRProvider
from .videocaptioner import VideoCaptionerASRProvider
from .faster_whisper import FasterWhisperASRProvider

__all__ = ["ASRProvider", "VideoASRProvider", "VideoCaptionerASRProvider", "FasterWhisperASRProvider"]
