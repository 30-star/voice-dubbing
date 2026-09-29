"""ElevenLabs HTTP adapter. The dubbing pipeline knows only TTSProvider."""

import json
import math
import os
import re
import socket
import time
import wave
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ..audio.dubbing import SAMPLE_RATE, normalize_speech, wav_frames
from ..errors import MediaError, ProviderError, ValidationError
from ..models import SpeechAudio, SpeechRequest


_API = "https://api.elevenlabs.io"
_MP3_FORMATS = {
    "mp3_22050_32", "mp3_44100_32", "mp3_44100_64", "mp3_44100_96",
    "mp3_44100_128", "mp3_44100_192",
}
_PCM_RATES = {8000, 16000, 22050, 24000, 44100}


def _format_details(output_format: str) -> tuple[str, int]:
    if output_format in _MP3_FORMATS:
        return "mp3", int(output_format.split("_")[1])
    match = re.fullmatch(r"pcm_(\d+)", output_format)
    if match and int(match.group(1)) in _PCM_RATES:
        return "pcm", int(match.group(1))
    raise ValidationError(f"unsupported ElevenLabs output_format: {output_format}")


def _retry_delay(headers, attempt: int) -> float:
    value = headers.get("Retry-After") if headers else None
    if value:
        try:
            return min(60.0, max(0.0, float(value)))
        except ValueError:
            try:
                then = parsedate_to_datetime(value)
                if then.tzinfo is None:
                    then = then.replace(tzinfo=timezone.utc)
                return min(60.0, max(0.0, (then - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    return min(60.0, float(2 ** (attempt - 1)))


def create_from_configuration(config):
    # Preserve the validated request contract; unsupported synthesis controls must not be ignored.
    if config.speed != 1 or config.parameters:
        raise ValidationError("ElevenLabs Adapter currently supports default speed=1 and no extra audio parameters")
    return ElevenLabsTTSProvider(model_id=config.model_id, output_format=config.output_format,
        timeout=config.timeout, max_retries=config.max_retries, ffmpeg_path=config.ffmpeg_path)


class ElevenLabsTTSProvider:
    provider_id = "elevenlabs"
    is_mock = False

    def __init__(
        self, *, api_key: str | None = None, model_id: str = "eleven_multilingual_v2",
        output_format: str = "mp3_44100_128", timeout: float = 60.0,
        max_retries: int = 2, ffmpeg_path: Path | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("ELEVENLABS_API_KEY")
        if not self._api_key or not self._api_key.strip():
            raise ProviderError("ELEVENLABS_API_KEY is not configured")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValidationError("model_id must be non-empty")
        self._codec, self._sample_rate = _format_details(output_format)
        if not isinstance(timeout, (float, int)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValidationError("timeout must be a positive finite number")
        if type(max_retries) is not int or not 0 <= max_retries <= 10:
            raise ValidationError("max_retries must be an integer from 0 to 10")
        self.model_id = model_id
        self.output_format = output_format
        self.timeout = float(timeout)
        self.max_retries = max_retries
        self.ffmpeg_path = ffmpeg_path
        self.selected_voice_name: str | None = None
        self.synthesis_http_requests = 0

    def _safe_detail(self, body: bytes) -> str:
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
            detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("status") or "request rejected"
            detail = str(detail)[:400].replace("\r", " ").replace("\n", " ")
        except (ValueError, TypeError):
            detail = "request rejected"
        return detail.replace(self._api_key, "[REDACTED]")

    def _request(self, request: Request, *, log_path: Path | None = None) -> bytes:
        log_lines: list[str] = []
        try:
            for attempt in range(1, self.max_retries + 2):
                try:
                    if request.get_method() == "POST":
                        self.synthesis_http_requests += 1
                    with urlopen(request, timeout=self.timeout) as response:
                        request_id = (response.headers.get("request-id") or response.headers.get("x-request-id") or "").replace(
                            self._api_key, "[REDACTED]"
                        )
                        # A read failure is ambiguous: the service may already have generated speech.
                        try:
                            data = response.read()
                        except (OSError, TimeoutError, HTTPException) as exc:
                            log_lines.append(f"attempt={attempt} status=response_interrupted request_id={request_id}")
                            raise ProviderError("ElevenLabs response interrupted or timed out after request") from exc
                        log_lines.append(f"attempt={attempt} status={response.status} request_id={request_id}")
                        if not data:
                            raise ProviderError("ElevenLabs returned an empty response")
                        return data
                except HTTPError as exc:
                    request_id = (exc.headers.get("request-id") or exc.headers.get("x-request-id") or "").replace(
                        self._api_key, "[REDACTED]"
                    )
                    try:
                        detail = self._safe_detail(exc.read(4096))
                    except (OSError, HTTPException):
                        detail = "request rejected"
                    log_lines.append(f"attempt={attempt} status={exc.code} request_id={request_id} detail={detail}")
                    if (exc.code == 429 or 500 <= exc.code <= 599) and attempt <= self.max_retries:
                        time.sleep(_retry_delay(exc.headers, attempt))
                        continue
                    raise ProviderError(f"ElevenLabs HTTP {exc.code}: {detail}") from exc
                except (URLError, socket.timeout, TimeoutError, OSError) as exc:
                    log_lines.append(f"attempt={attempt} status=connection_error error={type(exc).__name__}")
                    raise ProviderError(f"ElevenLabs connection failed or timed out: {type(exc).__name__}") from exc
            raise AssertionError("retry loop exhausted")
        finally:
            if log_path is not None:
                log_path.write_text("\n".join(log_lines) + ("\n" if log_lines else ""), encoding="utf-8")

    def select_voice(self) -> str:
        """Choose one API-usable premade voice, preferring Mandarin verification."""
        voices: list[dict] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            query = {"page_size": "100"}
            if page_token:
                query["next_page_token"] = page_token
            request = Request(
                f"{_API}/v2/voices?{urlencode(query)}",
                headers={"xi-api-key": self._api_key, "Accept": "application/json"},
            )
            try:
                page = json.loads(self._request(request))
                if not isinstance(page, dict) or not isinstance(page.get("voices"), list):
                    raise ValueError("missing voices list")
            except (ValueError, TypeError) as exc:
                raise ProviderError("ElevenLabs returned an invalid voices list") from exc
            voices.extend(voice for voice in page["voices"] if isinstance(voice, dict) and voice.get("voice_id"))
            if not page.get("has_more"):
                break
            page_token = page.get("next_page_token")
            if not isinstance(page_token, str) or not page_token or page_token in seen_tokens:
                raise ProviderError("ElevenLabs voices pagination did not advance")
            seen_tokens.add(page_token)
        if not voices:
            raise ProviderError("ElevenLabs account has no available voices")

        def rank(voice: dict) -> tuple[int, str, str]:
            verified = voice.get("verified_languages") or []
            labels = voice.get("labels") or {}
            chinese_verified = any(
                isinstance(item, dict) and str(item.get("language", "")).lower().startswith("zh")
                for item in verified
            )
            chinese_label = any(
                "chinese" in str(value).lower() or str(value).lower().startswith("zh")
                or "中文" in str(value) or "普通话" in str(value)
                for value in labels.values()
            ) if isinstance(labels, dict) else False
            category = str(voice.get("category", "")).lower()
            # Free accounts can list library voices but cannot synthesize with them via API.
            priority = (0 if chinese_verified else 1 if chinese_label else 2) if category == "premade" else 3
            return priority, str(voice.get("name", "")).casefold(), str(voice["voice_id"])

        candidates = [voice for voice in voices if rank(voice)[0] < 3]
        if not candidates:
            raise ProviderError("ElevenLabs account has no premade voice for auto selection; pass --voice explicitly")
        chosen = min(candidates, key=rank)
        self.selected_voice_name = str(chosen.get("name") or chosen["voice_id"])
        return str(chosen["voice_id"])

    def synthesize(self, request: SpeechRequest, *, output_dir: Path) -> SpeechAudio:
        if not isinstance(request, SpeechRequest):
            raise ValidationError("request must be a SpeechRequest")
        output_dir.mkdir(parents=True, exist_ok=True)
        payload = {"text": request.text, "model_id": self.model_id}
        if request.language and self.model_id != "eleven_multilingual_v2":
            payload["language_code"] = request.language
        url = f"{_API}/v1/text-to-speech/{quote(request.voice_id, safe='')}?{urlencode({'output_format': self.output_format})}"
        http_request = Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"xi-api-key": self._api_key, "Content-Type": "application/json", "Accept": "audio/mpeg, audio/*"},
        )
        raw = self._request(http_request, log_path=output_dir / "elevenlabs.log")
        original = output_dir / ("speech.mp3" if self._codec == "mp3" else "speech.pcm")
        original.write_bytes(raw)
        normalized = output_dir / "speech.wav"
        if self._codec == "pcm":
            if len(raw) % 2:
                raise MediaError("ElevenLabs returned an incomplete PCM sample")
            source = output_dir / "source.wav"
            with wave.open(str(source), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(self._sample_rate)
                audio.writeframes(raw)
        else:
            source = original
        frames = normalize_speech(source, normalized, output_dir / "decode.log", ffmpeg_path=self.ffmpeg_path)
        return SpeechAudio(request.segment_id, normalized, math.ceil(frames / 24), SAMPLE_RATE, 1)
