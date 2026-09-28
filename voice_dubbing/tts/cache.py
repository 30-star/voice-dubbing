"""Persistent, provider-neutral speech cache with task-local copies on a hit."""

import hashlib
import json
import shutil
import wave
from pathlib import Path
from uuid import uuid4

from ..errors import ProviderError, ValidationError, VoiceDubbingError
from ..models import SpeechAudio, SpeechRequest
from .base import TTSProvider


_CACHE_VERSION = 1
_AUDIO_SUFFIXES = {".wav", ".mp3", ".pcm", ".m4a", ".ogg", ".flac"}


def _digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


class CachedTTSProvider:
    """Decorates a TTSProvider; never shares a task's output path with another task."""

    def __init__(self, provider: TTSProvider, cache_dir: Path, *, enabled: bool = True,
                 settings: dict | None = None) -> None:
        self.provider = provider
        self.cache_dir = cache_dir.resolve()
        self.enabled = enabled
        self.settings = settings or {}
        if not isinstance(self.settings, dict) or any(
            not isinstance(key, str) or any(secret in key.casefold()
                                            for secret in ("api_key", "secret", "token", "password"))
            for key in self.settings
        ):
            raise ValidationError("cache settings must be a mapping of non-secret TTS parameters")
        self.provider_id = provider.provider_id
        self.is_mock = bool(getattr(provider, "is_mock", False))
        self.model_id = getattr(provider, "model_id", None)
        self.output_format = getattr(provider, "output_format", None)
        self.selected_voice_name = getattr(provider, "selected_voice_name", None)
        self.cache_hits = 0
        self.provider_calls = 0
        self.warnings: list[str] = []
        self.cache_events: list[dict] = []

    @property
    def supplier_requests(self) -> int:
        return int(getattr(self.provider, "synthesis_http_requests", self.provider_calls))

    def _entry(self, request: SpeechRequest) -> Path:
        payload = {
            "version": _CACHE_VERSION,
            "text": request.text,
            "provider": self.provider_id,
            "voice_id": request.voice_id,
            "model_id": self.model_id,
            "output_format": self.output_format,
            "language": request.language,
            "tts_parameters": self.settings,
        }
        key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                        separators=(",", ":")).encode("utf-8")).hexdigest()
        return self.cache_dir / key[:2] / key

    def _validated(self, entry: Path) -> dict | None:
        try:
            manifest = json.loads((entry / "manifest.json").read_text(encoding="utf-8"))
            files = manifest["files"]
            audio_name = manifest["audio_name"]
            if manifest["version"] != _CACHE_VERSION or not isinstance(files, dict) or audio_name not in files:
                return None
            for name, expected_hash in files.items():
                if not isinstance(name, str) or name != Path(name).name or Path(name).suffix.lower() not in _AUDIO_SUFFIXES:
                    return None
                path = entry / name
                if not path.is_file() or path.resolve().parent != entry.resolve() or _digest(path) != expected_hash:
                    return None
                if path.suffix.lower() == ".wav":
                    with wave.open(str(path), "rb") as audio:
                        if audio.getnframes() <= 0:
                            return None
            SpeechAudio("cache-check", entry / audio_name, manifest["duration_ms"],
                        manifest["sample_rate_hz"], manifest["channels"])
            return manifest
        except (OSError, ValueError, KeyError, TypeError, wave.Error, VoiceDubbingError):
            return None

    def _restore(self, entry: Path, request: SpeechRequest, output_dir: Path) -> SpeechAudio | None:
        manifest = self._validated(entry)
        if manifest is None:
            return None
        output_dir.mkdir(parents=True, exist_ok=True)
        for name in manifest["files"]:
            shutil.copy2(entry / name, output_dir / name)
        (output_dir / "cache.log").write_text(f"hit={entry.name}\n", encoding="utf-8")
        return SpeechAudio(request.segment_id, output_dir / manifest["audio_name"],
                           manifest["duration_ms"], manifest["sample_rate_hz"], manifest["channels"])

    def _publish(self, entry: Path, speech: SpeechAudio, output_dir: Path) -> None:
        source = speech.audio_path.resolve()
        if source.parent != output_dir.resolve() or not source.is_file():
            raise ProviderError("TTS provider returned audio outside its segment directory")
        files = [path for path in output_dir.iterdir()
                 if path.is_file() and path.suffix.lower() in _AUDIO_SUFFIXES]
        if not files:
            raise ProviderError("TTS provider produced no cacheable audio")
        entry.parent.mkdir(parents=True, exist_ok=True)
        staging = entry.parent / f".{entry.name}-{uuid4().hex}.partial"
        staging.mkdir()
        try:
            hashes = {}
            for path in files:
                shutil.copy2(path, staging / path.name)
                hashes[path.name] = _digest(staging / path.name)
            manifest = {
                "version": _CACHE_VERSION,
                "audio_name": source.name,
                "duration_ms": speech.duration_ms,
                "sample_rate_hz": speech.sample_rate_hz,
                "channels": speech.channels,
                "files": hashes,
            }
            (staging / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
            )
            # The first completed writer wins. A corrupt old entry is replaced.
            if entry.exists():
                if self._validated(entry) is not None:
                    return
                shutil.rmtree(entry)
            staging.replace(entry)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def is_cached(self, request: SpeechRequest) -> bool:
        """Read-only preflight, useful when only selected cache misses may generate speech."""
        return self.enabled and self._validated(self._entry(request)) is not None

    def synthesize(self, request: SpeechRequest, *, output_dir: Path) -> SpeechAudio:
        hits_before, calls_before, http_before = self.cache_hits, self.provider_calls, self.supplier_requests
        succeeded = False
        try:
            speech = self._synthesize(request, output_dir=output_dir)
            succeeded = True
            return speech
        finally:
            attempted = self.provider_calls > calls_before
            self.cache_events.append({
                "segment_id": request.segment_id,
                "text_sha256": hashlib.sha256(request.text.encode("utf-8")).hexdigest(),
                "cache_hit": self.cache_hits > hits_before,
                "generation_attempted": attempted,
                "generated": attempted and succeeded,
                "supplier_requests": self.supplier_requests - http_before,
                "status": "succeeded" if succeeded else "failed",
            })

    def _synthesize(self, request: SpeechRequest, *, output_dir: Path) -> SpeechAudio:
        entry = self._entry(request) if self.enabled else None
        if entry is not None and entry.is_dir():
            try:
                restored = self._restore(entry, request, output_dir)
            except OSError as exc:
                self.warnings.append(f"speech cache read failed: {type(exc).__name__}: {exc}")
                if output_dir.is_dir():
                    for path in output_dir.iterdir():
                        if path.is_file() and path.suffix.lower() in _AUDIO_SUFFIXES:
                            path.unlink(missing_ok=True)
                restored = None
            if restored is not None:
                self.cache_hits += 1
                return restored
        self.provider_calls += 1
        speech = self.provider.synthesize(request, output_dir=output_dir)
        if speech.segment_id != request.segment_id:
            raise ProviderError("TTS provider returned the wrong segment id")
        if entry is not None:
            try:
                self._publish(entry, speech, output_dir)
            except (OSError, ValueError, TypeError, ProviderError) as exc:
                self.warnings.append(f"speech cache write failed: {type(exc).__name__}: {exc}")
        return speech
