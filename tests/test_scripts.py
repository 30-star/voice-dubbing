import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from voice_dubbing.errors import ValidationError
from voice_dubbing.models import TranscriptSegment, TranscriptTimeline
from voice_dubbing.models.transcript import timeline_sha256
from voice_dubbing.correction.operations import create_correction
from voice_dubbing.scripts import (copy_script, create_script, inspect_script, load_script,
    replace_script_text, resolve_script, save_script, script_from_dict, script_to_dict,
    set_segment_text, restore_script_segment, rename_script, migrate_script)
from voice_dubbing.timeline import save_timeline


def timeline():
    return TranscriptTimeline((TranscriptSegment("1", 100, 500, "你好你好"),
                               TranscriptSegment("2", 600, 1000, "你好世界")), 1200)


def reviewed():
    return replace(create_correction(timeline()), reviewed=True)


def variant(base=None, **kwargs):
    return create_script(base or reviewed(), base_path=Path("corrected_timeline.json"),
                         id=kwargs.get("id", "original"), name=kwargs.get("name", "原版"))


class ScriptTests(unittest.TestCase):
    def test_copy_and_edit_preserve_original_and_timing(self):
        base = reviewed()
        original = variant(base)
        copied = copy_script(original, id="revised", name="修改版")
        edited = set_segment_text(copied, "2", "你好朋友", base=base)
        resolved = resolve_script(base, edited)
        self.assertEqual(dict(original.text_overrides), {})
        self.assertEqual(dict(copied.text_overrides), {})
        self.assertEqual(dict(edited.text_overrides), {"2": "你好朋友"})
        self.assertEqual(base.to_timeline(), timeline())
        self.assertEqual([(s.id, s.start_ms, s.end_ms) for s in resolved.segments],
                         [(s.id, s.start_ms, s.end_ms) for s in base.segments])
        self.assertEqual(resolved.duration_ms, base.duration_ms)
        self.assertEqual(inspect_script(base, edited)["changed_segments"], 1)
        with self.assertRaises(TypeError):
            edited.text_overrides["2"] = "changed"
        with self.assertRaisesRegex(ValidationError, "new variant"):
            copy_script(original, id="ORIGINAL", name="copy")

    def test_replace_is_literal_global_and_transactional(self):
        base, original = reviewed(), variant()
        result, count = replace_script_text(original, "你好", "再见", base=base)
        self.assertEqual(count, 3)
        self.assertEqual([s.text for s in resolve_script(base, result).segments], ["再见再见", "再见世界"])
        no_change, count = replace_script_text(original, "不存在", "else", base=base)
        self.assertEqual(count, 0)
        self.assertIs(no_change, original)
        for find, replacement in (("你好", ""), ("", "x")):
            with self.assertRaises(ValidationError):
                replace_script_text(original, find, replacement, base=base)
        with self.assertRaisesRegex(ValidationError, "unknown"):
            set_segment_text(original, "3", "new", base=base)
        with self.assertRaises(ValidationError):
            set_segment_text(original, "1", " ", base=base)
        self.assertEqual(dict(original.text_overrides), {})

    def test_binding_rejects_raw_time_order_count_changes(self):
        base, script = reviewed(), variant()
        invalid = [replace(base, duration_ms=1300), replace(base, segments=base.segments[:1]),
                   replace(base, raw_timeline_sha256="0" * 64),
                   replace(base, segments=(replace(base.segments[0], start_ms=101), base.segments[1]))]
        for bad in invalid:
            with self.subTest(bad=bad), self.assertRaisesRegex(ValidationError, "fingerprint"):
                resolve_script(bad, script)
        with self.assertRaisesRegex(ValidationError, "unknown"):
            resolve_script(base, replace(script, text_overrides={"3": "new"}))
        with self.assertRaisesRegex(ValidationError, "reviewed"):
            resolve_script(replace(base, reviewed=False), script)

    def test_strict_chinese_json_roundtrip_and_no_time_fields(self):
        script = set_segment_text(variant(id="中文版本"), "1", "中文测试", base=reviewed())
        data = script_to_dict(script)
        self.assertEqual(script_from_dict(data), script)
        self.assertEqual(data["schema_version"], 2)
        self.assertNotIn("segments", data)
        data["start_ms"] = 100
        with self.assertRaisesRegex(ValidationError, "unexpected"):
            script_from_dict(data)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "中文 版本.json"
            save_script(script, path)
            self.assertEqual(load_script(path), script)
            self.assertIn("中文测试", path.read_text(encoding="utf-8"))
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ValidationError):
                load_script(path)

    def test_atomic_failure_and_output_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "timeline.json"
            save_timeline(timeline(), source)
            before = source.read_bytes()
            script = variant()
            with self.assertRaisesRegex(ValidationError, "already exists"):
                save_script(script, source)
            path = root / "variant.json"
            save_script(script, path)
            old = path.read_bytes()
            with patch("voice_dubbing.scripts.serialization.os.replace", side_effect=OSError("injected IO failure")):
                with self.assertRaises(OSError):
                    save_script(set_segment_text(script, "2", "new", base=reviewed()), path, overwrite=True)
            self.assertEqual(path.read_bytes(), old)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(list(root.glob(".script-*.partial")), [])

    def test_corrected_updates_are_inherited_without_changing_overrides(self):
        base = reviewed()
        script = set_segment_text(variant(), "2", "覆盖文字", base=base)
        changed = replace(base, segments=tuple(replace(s, text="基础更新" + s.id) for s in base.segments))
        self.assertEqual([s.text for s in resolve_script(changed, script).segments], ["基础更新1", "覆盖文字"])
        self.assertEqual(dict(script.text_overrides), {"2": "覆盖文字"})
        self.assertEqual(resolve_script(changed, variant()).segments[1].text, "基础更新2")

    def test_restore_and_setting_base_text_remove_override(self):
        base = reviewed()
        changed = set_segment_text(variant(), "1", "覆盖", base=base)
        self.assertEqual(dict(restore_script_segment(changed, "1", base=base).text_overrides), {})
        self.assertEqual(dict(set_segment_text(changed, "1", base.segments[0].text, base=base).text_overrides), {})
        with self.assertRaisesRegex(ValidationError, "unknown"):
            restore_script_segment(changed, "missing", base=base)

    def test_timestamps_update_only_on_actual_changes(self):
        with patch("voice_dubbing.scripts.operations.utc_now", return_value="2026-09-28T00:00:00Z"):
            script = variant()
        with patch("voice_dubbing.scripts.operations.utc_now", return_value="2026-09-28T01:00:00Z"):
            changed = rename_script(script, "新名称")
            copied = copy_script(script, id="copy", name="Copy")
            edited = set_segment_text(script, "1", "changed", base=reviewed())
        self.assertEqual(changed.id, script.id)
        self.assertEqual(changed.created_at, script.created_at)
        self.assertEqual(changed.updated_at, "2026-09-28T01:00:00Z")
        self.assertEqual(copied.created_at, copied.updated_at)
        self.assertNotEqual(copied.created_at, script.created_at)
        self.assertEqual(edited.updated_at, changed.updated_at)
        self.assertIs(rename_script(script, script.name), script)
        self.assertIs(set_segment_text(script, "1", "你好你好", base=reviewed()), script)
        for value in ("bad", "2026-01-01T00:00:00+08:00", "2026-01-01T00:00:00"):
            with self.assertRaises(ValidationError):
                replace(script, updated_at=value)

    def test_duplicate_json_keys_and_invalid_override_values_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.json"
            serialized = json.dumps(script_to_dict(variant()), ensure_ascii=False)
            path.write_text(serialized.replace('"text_overrides": {}', '"text_overrides": {"1":"a","1":"b"}'), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "duplicate JSON key"):
                load_script(path)
        for overrides in ({"1": " "}, {"1": {"text": "x", "start_ms": 1}}, [], {1: "x"}):
            with self.assertRaises(ValidationError):
                replace(variant(), text_overrides=overrides)

    def test_references_rebase_across_directories_and_drives(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            script = create_script(reviewed(), base_path=root / "base" / "corrected_timeline.json", id="o", name="O")
            first = root / "one" / "original.json"
            second = root / "other" / "deep" / "copy.json"
            save_script(script, first)
            copied = copy_script(load_script(first), id="copy", name="C")
            save_script(copied, second)
            self.assertEqual(load_script(second).base_timeline, script.base_timeline)
            self.assertFalse(Path(json.loads(first.read_text())["base_timeline"]["path"]).is_absolute())
            with patch("voice_dubbing.scripts.serialization.os.path.relpath", side_effect=ValueError("different drives")):
                self.assertEqual(script_to_dict(script, destination=second)["base_timeline"]["path"], str(script.base_timeline.path))

    def test_legacy_migration_validates_identity_and_complete_order(self):
        base = reviewed()
        data = {"id": "old", "name": "旧版", "source_timeline_sha256": timeline_sha256(base.to_timeline()),
                "segments": [{"id": "1", "text": "你好你好"}, {"id": "2", "text": "改写"}]}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "old.json"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            old = path.read_bytes()
            with self.assertRaisesRegex(ValidationError, "migrate"):
                load_script(path)
            result = migrate_script(path, base, base_path=root / "corrected_timeline.json")
            self.assertEqual(dict(result.text_overrides), {"2": "改写"})
            self.assertEqual(path.read_bytes(), old)
            for segments in (data["segments"][:1], list(reversed(data["segments"])), [data["segments"][0]] * 2):
                path.write_text(json.dumps({**data, "segments": segments}), encoding="utf-8")
                with self.assertRaisesRegex(ValidationError, "count and order"):
                    migrate_script(path, base, base_path=root / "corrected_timeline.json")
            path.write_text(json.dumps({**data, "source_timeline_sha256": "0"*64}), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "recreate"):
                migrate_script(path, base, base_path=root / "corrected_timeline.json")

    def test_replace_preserves_unchanged_overrides_when_base_catches_up(self):
        base = reviewed()
        script = set_segment_text(variant(), "2", "新的基础文字", base=base)
        updated = replace(base, segments=(base.segments[0], replace(base.segments[1], text="新的基础文字")))
        same, count = replace_script_text(script, "基础", "基础", base=updated)
        self.assertEqual(count, 1)
        self.assertIs(same, script)
        changed, count = replace_script_text(script, "你好", "再见", base=updated)
        self.assertEqual(count, 2)
        self.assertEqual(changed.text_overrides["2"], "新的基础文字")
        self.assertEqual(changed.text_overrides["1"], "再见再见")
