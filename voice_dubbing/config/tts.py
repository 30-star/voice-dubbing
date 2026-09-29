"""Non-secret, provider-specific synthesis configuration."""

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from ..errors import ValidationError


def validate_audio_parameters(value):
    """Accept JSON audio parameters, never credential-bearing settings."""
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or any(word in key.casefold() for word in
                                              ("key", "secret", "token", "password", "authorization")):
                raise ValidationError("TTS parameters must contain only non-secret audio settings")
            validate_audio_parameters(child)
    elif isinstance(value, list):
        for child in value:
            validate_audio_parameters(child)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError("TTS parameter numbers must be finite")
    elif value is not None and type(value) not in (str, int, bool):
        raise ValidationError("TTS parameters must be JSON values")


@dataclass(frozen=True)
class TTSConfiguration:
    provider: str
    model_id: str | None = None
    output_format: str | None = None
    timeout: float = 60.0
    max_retries: int = 2
    speed: float = 1.0
    parameters: dict = field(default_factory=dict)
    ffmpeg_path: Path | None = None

    def __post_init__(self):
        if not self.provider or not isinstance(self.provider, str):
            raise ValidationError("TTS provider must be non-empty")
        for name in ("model_id", "output_format"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValidationError(f"{name} must be non-empty")
        for name in ("timeout", "speed"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValidationError(f"{name} must be a positive finite number")
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 10:
            raise ValidationError("max_retries must be an integer from 0 to 10")
        if not isinstance(self.parameters, dict):
            raise ValidationError("parameters must be a mapping")
        validate_audio_parameters(self.parameters)
        if "speed" in self.parameters:
            raise ValidationError("configure speed as a top-level TTS option")
        object.__setattr__(self, "parameters", json.loads(json.dumps(self.parameters)))

    @property
    def cache_parameters(self):
        return {**self.parameters, "speed": float(self.speed)}


def load_provider_settings(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValidationError("duplicate TTS configuration field")
                result[key] = value
            return result
        value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValidationError("cannot read TTS configuration file") from exc
    allowed = {"model_id", "output_format", "timeout", "max_retries", "speed", "parameters"}
    if not isinstance(value, dict) or set(value) != {"providers"} or not isinstance(value["providers"], dict):
        raise ValidationError("TTS configuration must contain a providers mapping")
    for provider, settings in value["providers"].items():
        if not isinstance(settings, dict) or set(settings) - allowed:
            raise ValidationError("provider configuration accepts only non-secret TTS options")
        if "parameters" in settings:
            validate_audio_parameters(settings["parameters"])
    return value["providers"]


def resolve_configuration(descriptor, *, model_id=None, options=None,
                          settings=None, environ: Mapping[str, str]) -> TTSConfiguration:
    options, settings = options or {}, settings or {}
    values = {}
    defaults = dict(model_id=descriptor.default_model, output_format=descriptor.default_format,
                    timeout=60.0, max_retries=2, speed=1.0)
    for name, default in defaults.items():
        explicit = model_id if name == "model_id" and model_id is not None else options.get(name)
        raw = (explicit if explicit is not None else settings.get(name,
               environ.get(f"{descriptor.env_prefix}_{name.upper()}", default)))
        if isinstance(raw, bool) or (name == "max_retries" and isinstance(raw, float) and not raw.is_integer()):
            raise ValidationError(f"invalid {descriptor.id} {name}")
        try:
            values[name] = (float(raw) if name in ("timeout", "speed") else
                            int(raw) if name == "max_retries" else raw)
        except (ValueError, TypeError):
            raise ValidationError(f"invalid {descriptor.id} {name}") from None
    return TTSConfiguration(descriptor.id, **values,
        parameters=settings.get("parameters", {}), ffmpeg_path=options.get("ffmpeg_path"))
