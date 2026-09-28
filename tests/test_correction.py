import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from voice_dubbing.correction import (corrected_from_dict, corrected_to_dict, create_correction,
    discard_correction, edit_correction, initialize_correction, inspect_correction, load_saved,
    resolve_tts_timeline, restore_all, restore_segment, save_correction, set_text)
from voice_dubbing.correction.serialization import write_json_atomic
from voice_dubbing.errors import ValidationError
from voice_dubbing.models import CorrectedSegment
from voice_dubbing.models.transcript import timeline_sha256
from voice_dubbing.timeline import load_timeline
from test_scripts import timeline


class CorrectionModelTests(unittest.TestCase):
    def test_text_only_models_are_immutable_and_preserve_timing(self):
        raw = timeline()
        baseline = create_correction(raw)
        edited = set_text(baseline, "2", "人工校正中文")
        self.assertFalse(baseline.reviewed)
        self.assertFalse(baseline.segments[1].edited)
        self.assertTrue(edited.segments[1].edited)
        self.assertEqual(edited.segments[1].original_text, raw.segments[1].text)
        self.assertEqual([(s.id, s.start_ms, s.end_ms) for s in edited.to_timeline().segments],
                         [(s.id, s.start_ms, s.end_ms) for s in raw.segments])
        self.assertEqual(restore_segment(edited, "2"), baseline)
        self.assertEqual(restore_all(edited), baseline)
        with self.assertRaises(FrozenInstanceError):
            edited.segments[1].start_ms = 800
        with self.assertRaises(ValidationError):
            set_text(baseline, "2", " \n ")
        with self.assertRaisesRegex(ValidationError, "unknown"):
            restore_segment(baseline, "missing")

    def test_binding_rejects_timing_original_text_ids_and_duration_changes(self):
        raw = timeline()
        corrected = create_correction(raw)
        bad_segments = [
            (replace(corrected.segments[0], start_ms=101), corrected.segments[1]),
            (replace(corrected.segments[0], original_text="其他原文"), corrected.segments[1]),
            (replace(corrected.segments[0], id="other"), corrected.segments[1]),
            corrected.segments[:1],
        ]
        for segments in bad_segments:
            with self.subTest(segments=segments), self.assertRaises(ValidationError):
                replace(corrected, segments=segments).validate_raw(raw)
        with self.assertRaises(ValidationError):
            replace(corrected, duration_ms=1300).validate_raw(raw)
        with self.assertRaisesRegex(ValidationError, "fingerprint"):
            replace(corrected, raw_timeline_sha256="0" * 64).validate_raw(raw)
        with self.assertRaisesRegex(ValidationError, "duplicate"):
            replace(corrected, segments=(corrected.segments[0], corrected.segments[0]))

    def test_strict_json_and_derived_edited_flag(self):
        corrected = set_text(create_correction(timeline()), "1", "这是校正结果")
        data = corrected_to_dict(corrected)
        self.assertEqual(corrected_from_dict(json.loads(json.dumps(data, ensure_ascii=False))), corrected)
        for mutation in (lambda d: d["segments"][0].update(edited=False),
                         lambda d: d.update(reviewed=1),
                         lambda d: d.update(schema_version=2),
                         lambda d: d["segments"][0].pop("original_text"),
                         lambda d: d.update(script_id="not-a-variant")):
            bad = json.loads(json.dumps(data))
            mutation(bad)
            with self.assertRaises(ValidationError):
                corrected_from_dict(bad)


class CorrectionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "中文 字幕"
        initialize_correction(timeline(), self.folder)
        self.raw_bytes = (self.folder / "raw_timeline.json").read_bytes()
        self.alias_bytes = (self.folder / "timeline.json").read_bytes()

    def assert_raw_preserved(self):
        self.assertEqual((self.folder / "raw_timeline.json").read_bytes(), self.raw_bytes)
        self.assertEqual((self.folder / "timeline.json").read_bytes(), self.alias_bytes)

    def test_draft_save_and_discard_are_distinct(self):
        saved_bytes = (self.folder / "corrected_timeline.json").read_bytes()
        edit_correction(self.folder, segment_id="2", text="修改结果")
        self.assertEqual((self.folder / "corrected_timeline.json").read_bytes(), saved_bytes)
        state = inspect_correction(self.folder)
        self.assertEqual(state["unsaved_segments"], 1)
        self.assertEqual(state["segments"][1]["text"], "你好世界")
        self.assertEqual(state["segments"][1]["draft_text"], "修改结果")
        discard_correction(self.folder)
        discard_correction(self.folder)
        self.assertEqual((self.folder / "corrected_timeline.json").read_bytes(), saved_bytes)
        edit_correction(self.folder, segment_id="2", text="修改结果")
        saved = save_correction(self.folder)
        self.assertTrue(saved.reviewed)
        self.assertTrue(saved.segments[1].edited)
        self.assertFalse((self.folder / "correction_draft.json").exists())
        self.assert_raw_preserved()

    def test_tts_requires_review_and_ignores_unsaved_text(self):
        with self.assertRaisesRegex(ValidationError, "review"):
            resolve_tts_timeline(self.folder)
        save_correction(self.folder)  # Confirming unchanged subtitles is a valid save.
        edit_correction(self.folder, segment_id="2", text="尚未保存")
        for path in (self.folder, *(self.folder / name for name in
                     ("raw_timeline.json", "timeline.json", "corrected_timeline.json"))):
            with self.subTest(path=path):
                resolved, source = resolve_tts_timeline(path)
                self.assertEqual(resolved, timeline())
                self.assertFalse(source["segments"][1]["edited"])
        with self.assertRaisesRegex(ValidationError, "requires"):
            resolve_tts_timeline(self.folder / "correction_draft.json")
        save_correction(self.folder)
        resolved, source = resolve_tts_timeline(self.folder)
        self.assertEqual(resolved.segments[1].text, "尚未保存")
        self.assertTrue(source["segments"][1]["edited"])
        self.assertEqual(source["corrected_timeline_sha256"], timeline_sha256(resolved))
        self.assert_raw_preserved()

    def test_restore_segment_and_all_require_save(self):
        edit_correction(self.folder, segment_id="1", text="新第一句")
        edit_correction(self.folder, segment_id="2", text="新第二句")
        save_correction(self.folder)
        edit_correction(self.folder, segment_id="1", restore=True)
        self.assertTrue(load_saved(self.folder).segments[0].edited)
        save_correction(self.folder)
        self.assertEqual([s.edited for s in load_saved(self.folder).segments], [False, True])
        edit_correction(self.folder, all_segments=True)
        self.assertTrue(load_saved(self.folder).segments[1].edited)
        save_correction(self.folder)
        self.assertEqual(load_saved(self.folder).to_timeline(), timeline())
        self.assertEqual(inspect_correction(self.folder)["edited_segments"], 0)
        self.assert_raw_preserved()

    def test_atomic_save_failure_preserves_saved_and_draft(self):
        edit_correction(self.folder, segment_id="2", text="新文字")
        before_saved = (self.folder / "corrected_timeline.json").read_bytes()
        before_draft = (self.folder / "correction_draft.json").read_bytes()
        with patch("voice_dubbing.correction.serialization.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                save_correction(self.folder)
            with self.assertRaises(OSError):
                edit_correction(self.folder, segment_id="1", text="无法写入的新草稿")
        self.assertEqual((self.folder / "corrected_timeline.json").read_bytes(), before_saved)
        self.assertEqual((self.folder / "correction_draft.json").read_bytes(), before_draft)
        self.assertEqual(list(self.folder.glob(".correction-*.partial")), [])
        self.assert_raw_preserved()

    def test_stale_draft_does_not_overwrite_saved_changes(self):
        edit_correction(self.folder, segment_id="2", text="草稿修改")
        # Another editor saved a different valid correction after this draft began.
        other = replace(set_text(load_saved(self.folder), "1", "其他编辑者"), reviewed=True)
        write_json_atomic(corrected_to_dict(other), self.folder / "corrected_timeline.json", overwrite=True)
        before = (self.folder / "corrected_timeline.json").read_bytes()
        self.assertTrue(inspect_correction(self.folder)["stale_draft"])
        with self.assertRaisesRegex(ValidationError, "stale"):
            save_correction(self.folder)
        self.assertEqual((self.folder / "corrected_timeline.json").read_bytes(), before)
        discard_correction(self.folder)
        self.assertEqual(load_saved(self.folder), other)

    def test_completed_save_recovers_after_draft_cleanup_failure(self):
        edit_correction(self.folder, segment_id="2", text="已提交")
        original_unlink = Path.unlink
        def unlink(path, **kwargs):
            if path.name == "correction_draft.json":
                raise OSError("cleanup failure")
            return original_unlink(path, **kwargs)
        with patch.object(Path, "unlink", unlink):
            saved = save_correction(self.folder)
        self.assertEqual(saved.segments[1].text, "已提交")
        self.assertFalse(inspect_correction(self.folder)["stale_draft"])
        edit_correction(self.folder, segment_id="1", text="下一次编辑")
        save_correction(self.folder)
        self.assertEqual([s.text for s in load_saved(self.folder).segments], ["下一次编辑", "已提交"])

    def test_corruption_and_output_protection(self):
        with self.assertRaisesRegex(ValidationError, "already exists"):
            initialize_correction(timeline(), self.folder)
        path = self.folder / "corrected_timeline.json"
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValidationError):
            resolve_tts_timeline(self.folder)
        path.unlink()
        with self.assertRaisesRegex(ValidationError, "missing"):
            resolve_tts_timeline(self.folder / "timeline.json")
        self.assertEqual(load_timeline(self.folder / "raw_timeline.json"), timeline())
        self.assert_raw_preserved()
