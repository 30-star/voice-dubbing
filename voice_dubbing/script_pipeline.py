"""One reviewed base snapshot × sparse variants × selected voices, without ASR."""

import json
from dataclasses import replace
from pathlib import Path

from .batch_pipeline import ProviderFactory, VideoRenderer, _result_dict, _safe_id, render_voice
from .errors import MediaError, ValidationError
from .models import ScriptBatchDubbingJob, ScriptBatchDubbingResult, ScriptVoiceResult, TranscriptSegment, TranscriptTimeline
from .models.script import script_sha256
from .models.transcript import timeline_sha256
from .renderer import render_dubbed_video
from .scripts import resolve_script, save_script, inspect_script
from .correction.serialization import corrected_to_dict, write_json_atomic
from .timeline.serialization import timeline_to_dict
from .timeline import save_timeline
from .tts.concurrent import validate_concurrency


def _report(job: ScriptBatchDubbingJob, output_dir: Path, results: list[ScriptVoiceResult],
            subtitle_source: dict | None = None) -> Path:
    total = sum(len(selection.voices) for selection in job.selections)
    successes = sum(item.result.status == "succeeded" for item in results)
    status = ("running" if len(results) < total else "succeeded" if successes == total
              else "partial_success" if successes else "failed")
    combinations = [{"script_id": item.script.id, "script_name": item.script.name,
                     "script_sha256": script_sha256(item.script),
                     **inspect_script(job.base_timeline, item.script), **_result_dict(item.result)}
                    for item in results]
    data = {
        "status": status, "source_video": str(job.source_video.resolve()),
        "timeline_path": str(output_dir / "timeline.json"),
        "source_timeline_sha256": timeline_sha256(job.timeline),
        "timeline_duration_ms": job.timeline.duration_ms,
        "asr_calls": 0, "audio_extraction_calls": 0,
        "raw_timeline_sha256": job.base_timeline.raw_timeline_sha256, "total_combinations": total, "completed_combinations": len(results),
        "cache_hits": sum(item.result.cache_hits for item in results),
        "generated_segments": sum(item.result.generated_segments for item in results),
        "supplier_requests": sum(item.result.supplier_requests for item in results),
        "subtitle_source": subtitle_source,
        "variants": [{"id": selection.script.id, "name": selection.script.name,
                      "script_sha256": script_sha256(selection.script),
                      **inspect_script(job.base_timeline, selection.script),
                      "script_path": str(output_dir / "variants" / _safe_id(selection.script.id) / "script_variant.json"),
                      "voices": [voice.id for voice in selection.voices]}
                     for selection in job.selections],
        "combinations": combinations,
    }
    report_path = output_dir / "script_batch_report.json"
    partial = output_dir / "script_batch_report.partial.json"
    partial.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    partial.replace(report_path)
    return report_path


def run_script_batch(
    job: ScriptBatchDubbingJob, *, output_dir: Path, provider_factory: ProviderFactory,
    language: str | None = None, ffmpeg_path: Path | None = None,
    ffprobe_path: Path | None = None, renderer: VideoRenderer = render_dubbed_video,
    subtitle_source: dict | None = None,
    tts_concurrency: int = 1,
) -> ScriptBatchDubbingResult:
    validate_concurrency(tts_concurrency)
    if not isinstance(job, ScriptBatchDubbingJob):
        raise ValidationError("job must be a ScriptBatchDubbingJob")
    if not job.source_video.is_file():
        raise MediaError(f"source video not found: {job.source_video}")
    output_dir = output_dir.resolve()
    if output_dir == job.source_video.resolve().parent:
        raise MediaError("output directory must differ from the source video directory")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise MediaError(f"output directory must be empty: {output_dir}")
    # Bind every variant before publishing output or invoking a TTS provider.
    prepared = [(selection, resolve_script(job.base_timeline, selection.script)) for selection in job.selections]
    names = [_safe_id(s.script.id).casefold() for s in job.selections]
    if len(set(names)) != len(names):
        raise ValidationError("script output directory collision")
    for selection in job.selections:
        names = [_safe_id(v.id).casefold() for v in selection.voices]
        if len(set(names)) != len(names):
            raise ValidationError("voice output directory collision")
    base_dir = output_dir / "base"
    raw = TranscriptTimeline(tuple(TranscriptSegment(s.id, s.start_ms, s.end_ms, s.original_text)
                                   for s in job.base_timeline.segments), job.base_timeline.duration_ms)
    job.base_timeline.validate_raw(raw)
    output_dir.mkdir(parents=True, exist_ok=True)
    save_timeline(job.timeline, output_dir / "timeline.json")
    write_json_atomic(timeline_to_dict(raw), base_dir / "raw_timeline.json")
    write_json_atomic(corrected_to_dict(job.base_timeline), base_dir / "corrected_timeline.json")
    variants_dir = output_dir / "variants"
    variants_dir.mkdir()
    for selection, timeline in prepared:
        script_dir = variants_dir / _safe_id(selection.script.id)
        script_dir.mkdir()
        exported = replace(selection.script, base_timeline=replace(selection.script.base_timeline,
                            path=base_dir / "corrected_timeline.json"))
        save_script(exported, script_dir / "script_variant.json")
        save_timeline(timeline, script_dir / "effective_timeline.json")
    results: list[ScriptVoiceResult] = []
    report_path = _report(job, output_dir, results, subtitle_source)
    for selection, timeline in prepared:
        script_dir = variants_dir / _safe_id(selection.script.id)
        for voice in selection.voices:
            name = f"{job.source_video.stem}_{_safe_id(selection.script.id)}_{_safe_id(voice.id)}.mp4"
            result = render_voice(
                job.source_video, timeline, voice, output_dir=script_dir / _safe_id(voice.id),
                provider_factory=provider_factory, video_name=name, language=language,
                ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path, renderer=renderer,
                subtitle_source=subtitle_source,
                tts_concurrency=tts_concurrency,
                script_context={**inspect_script(job.base_timeline, selection.script),
                                "script_sha256": script_sha256(selection.script),
                                "voice_profile_id": voice.id, "voice_name": voice.name},
            )
            results.append(ScriptVoiceResult(selection.script, result))
            report_path = _report(job, output_dir, results, subtitle_source)
    successes = sum(item.result.status == "succeeded" for item in results)
    status = "succeeded" if successes == len(results) else "partial_success" if successes else "failed"
    return ScriptBatchDubbingResult(job, status, tuple(results), report_path)
