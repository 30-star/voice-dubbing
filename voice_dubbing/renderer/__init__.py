"""Video rendering with FFmpeg, separate from ASR and TTS."""

from .video import RenderedVideo, render_dubbed_video

__all__ = ["RenderedVideo", "render_dubbed_video"]
