"""Noiz v1 HTTP adapter; no provider details escape into dubbing orchestration."""

import json
import math
import os
import hashlib
import re
import time
from threading import Lock
from weakref import WeakValueDictionary
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from ..audio.dubbing import SAMPLE_RATE, normalize_speech
from ..errors import ProviderError, ValidationError
from ..models import SpeechAudio, SpeechRequest, VoiceProfile
from ..config.tts import TTSConfiguration, validate_audio_parameters

_API = "https://noiz.ai/v1"
MODEL_ID = "noiz-v1"  # Route identity for internal profiles/cache; NOT an API model parameter.
_DEFAULT_PARAMETERS = dict(quality_preset=3, duration=0, similarity_enh=False, trim_silence=False)
_ALLOWED_PARAMETERS = set(_DEFAULT_PARAMETERS) | {"emo"}


class _RateGate:
    """Share an account's cooldown across independent concurrent adapters."""
    def __init__(self):
        self.lock = Lock()
        self.until = 0.0

    def defer(self, seconds):
        with self.lock:
            self.until = max(self.until, time.monotonic() + seconds)

    def wait(self):
        while True:
            with self.lock:
                remaining = self.until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(60.0, remaining))


_RATE_GATES = WeakValueDictionary()
_RATE_GATES_LOCK = Lock()


def _rate_gate(api_key):
    # The dictionary stores only a digest, never the credential itself.
    identity = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    with _RATE_GATES_LOCK:
        gate = _RATE_GATES.get(identity)
        if gate is None:
            gate = _RateGate()
            _RATE_GATES[identity] = gate
        return gate


def _json_rate_limit(payload, detail):
    if re.search(r"insufficient credits|payment method|monthly quota|quota exceeded|unauthori[sz]ed", detail,
                 re.IGNORECASE):
        return False
    return (isinstance(payload, dict) and payload.get("code") in (429, "429")) or bool(
        re.search(r"\brate[ -]limit(?:ed)?\b|\btoo many requests\b", detail, re.IGNORECASE))


def _json_retry_delay(payload, detail, headers, attempt):
    value = payload.get("retry_after") if isinstance(payload, dict) else None
    if value is None and isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        value = payload["data"].get("retry_after")
    if value is None:
        match = re.search(r"retry\s+(?:in|after)\s+(\d+(?:\.\d+)?)\s*seconds?", detail, re.IGNORECASE)
        value = match.group(1) if match else None
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            return min(60.0, seconds)
    except (ValueError, TypeError):
        pass
    return _delay(headers, attempt)


def create_from_configuration(config):
    return NoizTTSProvider(model_id=config.model_id, output_format=config.output_format,
                           speed=config.speed, parameters=config.parameters,
                           timeout=config.timeout, max_retries=config.max_retries,
                           ffmpeg_path=config.ffmpeg_path)


def _multipart(fields):
    boundary = "voice-dubbing-" + uuid4().hex
    parts = []
    for name, value in fields.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n')
    parts.append(f"--{boundary}--\r\n")
    return "".join(parts).encode("utf-8"), boundary


def _delay(headers, attempt):
    value = headers.get("Retry-After") if headers else None
    try:
        return min(60.0, max(0.0, float(value))) if value else min(60.0, 2.0 ** (attempt - 1))
    except (ValueError, TypeError):
        try:
            then = parsedate_to_datetime(value)
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
            return min(60.0, max(0.0, (then - datetime.now(timezone.utc)).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return min(60.0, 2.0 ** (attempt - 1))


class NoizTTSProvider:
    provider_id = "noiz"
    is_mock = False

    def __init__(self, *, api_key=None, model_id=MODEL_ID, output_format="wav", speed=1.0,
                 parameters=None, timeout=60.0, max_retries=2, ffmpeg_path: Path | None = None):
        self._api_key = api_key if api_key is not None else os.environ.get("NOIZ_API_KEY")
        if not isinstance(self._api_key, str) or not self._api_key.strip():
            raise ProviderError("NOIZ_API_KEY is not configured")
        if "\r" in self._api_key or "\n" in self._api_key:
            raise ValidationError("NOIZ_API_KEY contains an invalid newline")
        # Reuse non-secret scalar validation, without adding anything to TTSProvider.
        config = TTSConfiguration("noiz", model_id=model_id, output_format=output_format,
                                  speed=speed, timeout=timeout, max_retries=max_retries,
                                  parameters={} if parameters is None else parameters)
        if model_id != MODEL_ID:
            raise ValidationError("Noiz v1 has no selectable model_id; use internal profile model noiz-v1")
        if output_format not in ("wav", "mp3"):
            raise ValidationError("Noiz output_format must be wav or mp3")
        if set(config.parameters) - _ALLOWED_PARAMETERS:
            raise ValidationError("unsupported Noiz audio parameters")
        self.parameters = {**_DEFAULT_PARAMETERS, **config.parameters}
        preset = self.parameters["quality_preset"]
        if type(preset) is not int or preset < 1:
            raise ValidationError("Noiz quality_preset must be a positive integer")
        duration = self.parameters["duration"]
        if type(duration) not in (int, float) or duration < 0:
            raise ValidationError("Noiz duration must be non-negative seconds")
        for name in ("similarity_enh", "trim_silence"):
            if type(self.parameters[name]) is not bool:
                raise ValidationError(f"Noiz {name} must be boolean")
        if "emo" in self.parameters:
            emotion = self.parameters["emo"]
            if isinstance(emotion, str):
                try:
                    emotion = json.loads(emotion)
                except ValueError as exc:
                    raise ValidationError("Noiz emo must be a JSON object") from exc
            if not isinstance(emotion, dict):
                raise ValidationError("Noiz emo must be a JSON object")
            validate_audio_parameters(emotion)
            if any(type(value) not in (int, float) or not 0 <= value <= 1 for value in emotion.values()):
                raise ValidationError("Noiz emotion strengths must be numbers from 0 to 1")
            self.parameters["emo"] = emotion
        self.model_id, self.output_format, self.speed = model_id, output_format, float(speed)
        self.timeout, self.max_retries, self.ffmpeg_path = float(timeout), max_retries, ffmpeg_path
        self.cache_parameters = {**self.parameters, "speed": self.speed, "stream": False}
        self.selected_voice_name = None
        self.synthesis_http_requests = 0
        self._rate_gate = _rate_gate(self._api_key)

    def _safe(self, value):
        return str(value).replace(self._api_key, "[REDACTED]").replace("\r", " ").replace("\n", " ")[:400]

    def _request(self, request, *, log_path=None):
        lines = []
        try:
            for attempt in range(1, self.max_retries + 2):
                try:
                    if request.get_method() == "POST":
                        self._rate_gate.wait()
                        self.synthesis_http_requests += 1
                    with urlopen(request, timeout=self.timeout) as response:
                        request_id = self._safe(response.headers.get("x-request-id", ""))
                        try:
                            data = response.read()
                        except (OSError, TimeoutError, HTTPException) as exc:
                            lines.append(f"attempt={attempt} status=response_interrupted request_id={request_id}")
                            raise ProviderError("Noiz response interrupted or timed out after request; not retried") from exc
                        lines.append(f"attempt={attempt} status={response.status} request_id={request_id}")
                        if not data:
                            raise ProviderError("Noiz returned an empty response")
                        if request.get_method() == "POST" and ("application/json" in response.headers.get("Content-Type", "")
                                                              or data.lstrip().startswith(b"{")):
                            try:
                                payload = json.loads(data)
                                detail = self._safe(payload.get("message", "expected audio binary"))
                            except (ValueError, AttributeError):
                                payload = None
                                detail = "expected audio binary"
                            if _json_rate_limit(payload, detail):
                                delay = _json_retry_delay(payload, detail, response.headers, attempt)
                                self._rate_gate.defer(delay)
                                lines.append(f"attempt={attempt} reason=rate_limit retry_after={delay:g}")
                                if attempt <= self.max_retries:
                                    continue
                            raise ProviderError(f"Noiz returned JSON instead of audio: {detail}")
                        return data
                except HTTPError as exc:
                    try:
                        body = json.loads(exc.read(4096))
                        detail = self._safe(body.get("message", "request rejected"))
                    except (ValueError, AttributeError, OSError, HTTPException):
                        detail = "request rejected"
                    lines.append(f"attempt={attempt} status={exc.code} detail={detail}")
                    if (exc.code == 429 or 500 <= exc.code <= 599) and attempt <= self.max_retries:
                        delay = _delay(exc.headers, attempt)
                        if exc.code == 429 and request.get_method() == "POST":
                            self._rate_gate.defer(delay)
                        else:
                            time.sleep(delay)
                        continue
                    raise ProviderError(f"Noiz HTTP {exc.code}: {detail}") from exc
                except (URLError, OSError, TimeoutError) as exc:
                    lines.append(f"attempt={attempt} status=connection_error error={type(exc).__name__}")
                    raise ProviderError(f"Noiz connection failed or timed out: {type(exc).__name__}; not retried") from exc
            raise AssertionError("retry loop exhausted")
        finally:
            if log_path is not None:
                log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def list_voices(self):
        """List built-in and account voices; no synthesis or random selection."""
        voices = {}
        for voice_type in ("built-in", "custom"):
            skip = 0
            while True:
                query = urlencode(dict(voice_type=voice_type, skip=skip, limit=100))
                raw = self._request(Request(f"{_API}/voices?{query}",
                    headers={"Authorization": self._api_key, "Accept": "application/json"}))
                try:
                    data = json.loads(raw)["data"]
                    rows, total = data["voices"], data["total_count"]
                    if not isinstance(rows, list) or type(total) is not int or total < 0:
                        raise ValueError("invalid pagination")
                    if not rows and (skip * 100) < total:
                        raise ValueError("missing voice page")
                    new = 0
                    for row in rows:
                        voice_id, name = row["voice_id"], row["display_name"]
                        if not isinstance(voice_id, str) or not voice_id.strip() or not isinstance(name, str) or not name.strip():
                            raise ValueError("invalid voice identity")
                        new += voice_id not in voices
                        voices[voice_id] = VoiceProfile("noiz-" + voice_id, name, "noiz", voice_id, self.model_id)
                except (ValueError, TypeError, KeyError) as exc:
                    raise ProviderError("Noiz returned an invalid voices list") from exc
                if (skip + 1) * 100 >= total:
                    break
                if not rows or new == 0 or skip >= 999:
                    raise ProviderError("Noiz voices pagination did not advance")
                skip += 1
        return tuple(sorted(voices.values(), key=lambda voice: (voice.name.casefold(), voice.voice_id)))

    def synthesize(self, request: SpeechRequest, *, output_dir: Path) -> SpeechAudio:
        if not isinstance(request, SpeechRequest):
            raise ValidationError("request must be a SpeechRequest")
        if len(request.text) > 5000:
            raise ValidationError("Noiz non-streaming text exceeds 5000 characters")
        output_dir.mkdir(parents=True, exist_ok=True)
        original = output_dir / f"speech.original.{self.output_format}"
        normalized = output_dir / "speech.wav"
        if original.exists() or normalized.exists():
            raise ProviderError("Noiz segment output already exists")
        fields = {**self.parameters, "text": request.text, "voice_id": request.voice_id,
                  "output_format": self.output_format, "speed": self.speed, "stream": False}
        if request.language:
            fields["target_lang"] = request.language
        body, boundary = _multipart(fields)
        raw = self._request(Request(f"{_API}/text-to-speech", data=body, method="POST",
            headers={"Authorization": self._api_key, "Content-Type": f"multipart/form-data; boundary={boundary}",
                     "Accept": "audio/wav, audio/mpeg"}), log_path=output_dir / "noiz.log")
        original.write_bytes(raw)
        # Decode and measure actual samples; never trust X-Audio-Duration alone.
        frames = normalize_speech(original, normalized, output_dir / "decode.log", ffmpeg_path=self.ffmpeg_path)
        return SpeechAudio(request.segment_id, normalized, math.ceil(frames / 24), SAMPLE_RATE, 1)
