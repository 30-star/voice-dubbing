"""CLI composition for editable script files and prepared script batches."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from .errors import MediaError, ValidationError
from .correction.store import resolve_corrected_timeline
from .correction.serialization import write_json_atomic
from .timeline.serialization import timeline_to_dict
from .models import ScriptBatchDubbingJob
from .script_pipeline import run_script_batch
from .scripts import (copy_script, create_script, inspect_script, load_script,
                      load_script_selections, replace_script_text, save_script, set_segment_text,
                      restore_script_segment, rename_script, resolve_script, migrate_script)
from .tts import CachedTTSProvider, ElevenLabsTTSProvider


def add_script_commands(subcommands) -> None:
    script = subcommands.add_parser("script", help="create and edit text-only script variants")
    commands = script.add_subparsers(dest="script_command", required=True)
    create = commands.add_parser("create", help="create sparse overrides over reviewed corrected subtitles")
    create.add_argument("timeline", type=Path)
    copy = commands.add_parser("copy", help="copy an existing variant with a new ID")
    copy.add_argument("script", type=Path)
    for command in (create, copy):
        command.add_argument("--id", required=True, dest="variant_id")
        command.add_argument("--name", required=True)
        command.add_argument("--output", required=True, type=Path)
    edit = commands.add_parser("set-text", help="replace one segment text")
    edit.add_argument("script", type=Path)
    edit.add_argument("--segment-id", required=True)
    edit.add_argument("--text", required=True)
    replace = commands.add_parser("replace", help="literal case-sensitive replace across all sentences")
    replace.add_argument("script", type=Path)
    replace.add_argument("--find", required=True)
    replace.add_argument("--replace", required=True, dest="replacement")
    inspect = commands.add_parser("inspect", help="validate binding and show text changes")
    inspect.add_argument("script", type=Path)
    restore = commands.add_parser("restore", help="inherit one segment from the base")
    restore.add_argument("script", type=Path)
    restore.add_argument("--segment-id", required=True)
    rename = commands.add_parser("rename", help="change the display name only")
    rename.add_argument("script", type=Path)
    rename.add_argument("--name", required=True)
    delete = commands.add_parser("delete", help="delete only this variant file")
    delete.add_argument("script", type=Path)
    resolve = commands.add_parser("resolve", help="export the effective Timeline")
    resolve.add_argument("script", type=Path)
    resolve.add_argument("--output", type=Path, required=True)
    migrate = commands.add_parser("migrate", help="explicitly migrate a legacy snapshot")
    migrate.add_argument("script", type=Path)
    migrate.add_argument("--timeline", type=Path, required=True)
    migrate.add_argument("--output", type=Path, required=True)
    for command in (copy, edit, replace, inspect, restore, resolve):
        command.add_argument("--timeline", type=Path)
    batch = subcommands.add_parser("dub-scripts", help="render each script × selected voices without ASR")
    batch.add_argument("video", type=Path)
    batch.add_argument("--timeline", type=Path, required=True)
    batch.add_argument("--variants-file", type=Path, required=True)
    batch.add_argument("--language")
    batch.add_argument("--output-format")
    batch.add_argument("--timeout", type=float)
    batch.add_argument("--max-retries", type=int)
    batch.add_argument("--ffmpeg", type=Path)
    batch.add_argument("--ffprobe", type=Path)
    batch.add_argument("--output-dir", type=Path)
    cache = batch.add_mutually_exclusive_group()
    cache.add_argument("--cache-dir", type=Path)
    cache.add_argument("--no-cache", action="store_true")


def run_script_command(args: argparse.Namespace) -> int:
    count = None
    command = args.script_command
    target = getattr(args, "output", None) or getattr(args, "script", None)
    if command in {"create", "migrate"}:
        base, source = resolve_corrected_timeline(args.timeline)
        base_path = Path(source["corrected_timeline_path"])
        variant = (create_script(base, base_path=base_path, id=args.variant_id, name=args.name)
                   if command == "create" else migrate_script(args.script, base, base_path=base_path))
        save_script(variant, target)
    else:
        original = load_script(args.script)
        if command == "delete":
            args.script.unlink()
            print(json.dumps({"deleted": str(args.script.resolve()), "id": original.id}, ensure_ascii=False))
            return 0
        if command == "rename":
            variant = rename_script(original, args.name)
        else:
            base, _ = resolve_corrected_timeline(args.timeline or original.base_timeline.path)
            if command == "inspect":
                print(json.dumps(inspect_script(base, original), ensure_ascii=False))
                return 0
            if command == "resolve":
                write_json_atomic(timeline_to_dict(resolve_script(base, original)), args.output)
                print(json.dumps({"timeline": str(args.output.resolve())}, ensure_ascii=False))
                return 0
            # Validate even when copy performs no text changes.
            resolve_script(base, original)
            if command == "copy":
                variant = copy_script(original, id=args.variant_id, name=args.name)
                save_script(variant, target)
            elif command == "restore":
                variant = restore_script_segment(original, args.segment_id, base=base)
            elif command == "set-text":
                variant = set_segment_text(original, args.segment_id, args.text, base=base)
            else:
                variant, count = replace_script_text(original, args.find, args.replacement, base=base)
        if command != "copy" and variant != original:
            save_script(variant, target, overwrite=True)
    result = {"script": str(target.resolve()), "id": variant.id, "name": variant.name,
              "schema_version": 2, "overrides": len(variant.text_overrides)}
    if count is not None:
        result["replacements"] = count
    print(json.dumps(result, ensure_ascii=False))
    return 0


def run_dub_scripts(args: argparse.Namespace, options: dict) -> int:
    selections = load_script_selections(args.variants_file)
    base, subtitle_source = resolve_corrected_timeline(args.timeline)
    job = ScriptBatchDubbingJob(args.video.resolve(), base, selections)
    if not args.video.is_file():
        raise MediaError(f"source video not found: {args.video}")
    root = Path(__file__).resolve().parents[1]
    output = args.output_dir or root / "output" / f"{args.video.stem[:40]}-scripts-{uuid4().hex[:8]}"
    if output.exists() and any(output.iterdir()):
        raise MediaError(f"output directory must be empty: {output}")
    # Constructor validation is local. Validate all profiles before any generation.
    for selection in selections:
        for voice in selection.voices:
            if voice.provider != "elevenlabs":
                raise ValidationError("dub-scripts currently supports ElevenLabs voice profiles only")
            ElevenLabsTTSProvider(model_id=voice.model_id, **options)
    cache_dir = args.cache_dir or root / "cache" / "tts"

    def factory(voice):
        provider = ElevenLabsTTSProvider(model_id=voice.model_id, **options)
        provider.selected_voice_name = voice.name
        return CachedTTSProvider(provider, cache_dir, enabled=not args.no_cache)

    result = run_script_batch(job, output_dir=output, provider_factory=factory,
                              language=args.language, ffmpeg_path=args.ffmpeg, ffprobe_path=args.ffprobe,
                              subtitle_source=subtitle_source)
    print(json.dumps({
        "status": result.status, "report": str(result.report_path), "asr_calls": 0,
        "combinations": [{"script_id": item.script.id, "script_name": item.script.name,
                          "voice_id": item.result.voice.id, "voice_name": item.result.voice.name,
                          "status": item.result.status,
                          "video": str(item.result.video_path) if item.result.video_path else None,
                          "cache_hits": item.result.cache_hits,
                          "generated_segments": item.result.generated_segments,
                          "supplier_requests": item.result.supplier_requests,
                          "error": item.result.error} for item in result.combinations],
    }, ensure_ascii=False))
    return 0 if result.status == "succeeded" else 3 if result.status == "partial_success" else 2
