"""Provider-separated raw ASR cache; never stores corrected subtitles."""
import hashlib
import json
import shutil
from pathlib import Path
from uuid import uuid4
from ..errors import ValidationError
from ..timeline import load_timeline, save_timeline


def _remove_owned_directory(path: Path, root: Path) -> None:
    if path.is_symlink():
        path.unlink()
        return
    resolved, boundary = path.resolve(), root.resolve()
    if resolved == boundary or not resolved.is_relative_to(boundary):
        raise OSError("refusing ASR cache cleanup outside its root")
    shutil.rmtree(resolved)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def cache_key(video: Path, provider, language: str | None) -> str:
    identity = {"version": 1, "source_sha256": file_sha256(video),
        "provider": provider.provider_id, "language": language or "auto",
        "parameters": getattr(provider, "cache_identity", {})}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def restore_cache(root: Path, key: str, output: Path):
    entry = root / key
    try:
        manifest = json.loads((entry / "manifest.json").read_text(encoding="utf-8"))
        if manifest["key"] != key:
            return None
        for name, expected in manifest["files"].items():
            if name not in {"timeline.json", "subtitle.srt"} or file_sha256(entry / name) != expected:
                return None
        if "timeline.json" not in manifest["files"]:
            return None
        timeline = load_timeline(entry / "timeline.json")
        if (entry / "subtitle.srt").exists():
            if "subtitle.srt" not in manifest["files"]:
                return None
            shutil.copyfile(entry / "subtitle.srt", output / "subtitle.srt")
        return timeline
    except (OSError, ValueError, KeyError, TypeError, ValidationError):
        return None


def publish_cache(root: Path, key: str, timeline, output: Path, *, provenance: dict | None = None) -> str | None:
    staging = root / f".{key}-{uuid4().hex}"
    try:
        staging.mkdir(parents=True)
        save_timeline(timeline, staging / "timeline.json")
        names = ["timeline.json"]
        if (output / "subtitle.srt").is_file():
            shutil.copyfile(output / "subtitle.srt", staging / "subtitle.srt")
            names.append("subtitle.srt")
        (staging / "manifest.json").write_text(json.dumps({"key": key, "provenance": provenance,
            "files": {name: file_sha256(staging / name) for name in names}}), encoding="utf-8")
        destination = root / key
        if destination.exists():
            old = root / f".{key}-old-{uuid4().hex}"
            destination.rename(old)
            try:
                staging.rename(destination)
            except OSError:
                old.rename(destination)
                raise
            _remove_owned_directory(old, root)
        else:
            staging.rename(destination)
        return None
    except OSError as exc:
        return f"ASR cache write failed: {exc}"
    finally:
        if staging.exists():
            try:
                _remove_owned_directory(staging, root)
            except OSError:
                pass
