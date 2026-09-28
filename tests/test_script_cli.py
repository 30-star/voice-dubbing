import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from voice_dubbing.cli import main
from voice_dubbing.correction import initialize_correction, save_correction
from voice_dubbing.scripts import create_script, load_script, save_script
from voice_dubbing.timeline import save_timeline
from test_cli import invoke
from test_scripts import timeline


class ScriptCLITests(unittest.TestCase):
    def test_invalid_later_variant_prevents_all_provider_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            source.touch()
            original_timeline = timeline()
            save_timeline(original_timeline, root / "timeline.json")
            initialize_correction(original_timeline, root / "review")
            base = save_correction(root / "review")
            save_script(create_script(base, base_path=root / "review" / "corrected_timeline.json", id="original", name="原版"), root / "original.json")
            bad = json.loads((root / "original.json").read_text(encoding="utf-8"))
            bad["id"] = "bad"
            bad["text_overrides"] = {"missing": "bad"}
            (root / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
            voices = [{"id": "bill", "name": "Bill", "provider": "elevenlabs",
                       "voice_id": "bill-id", "model_id": "model"}]
            (root / "jobs.json").write_text(json.dumps([
                {"script": "original.json", "voices": voices},
                {"script": "bad.json", "voices": voices},
            ]), encoding="utf-8")
            with patch("voice_dubbing.script_cli.ElevenLabsTTSProvider") as provider, \
                    contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                result = main(["dub-scripts", str(source), "--timeline", str(root / "review"),
                               "--variants-file", str(root / "jobs.json"), "--output-dir", str(root / "out")])
            self.assertEqual(result, 2)
            provider.assert_not_called()
            self.assertFalse((root / "out").exists())

    def test_edit_workflow_and_inspect_preserve_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "中文 timeline.json"
            original = root / "原版.json"
            revised = root / "修改版.json"
            save_timeline(timeline(), source)
            before = source.read_bytes()
            initialize_correction(timeline(), root / "review")
            save_correction(root / "review")
            commands = [
                ("script", "create", str(root / "review"), "--id", "original", "--name", "原版", "--output", str(original)),
                ("script", "copy", str(original), "--id", "revised", "--name", "修改版", "--output", str(revised)),
                ("script", "set-text", str(revised), "--segment-id", "2", "--text", "你好朋友"),
                ("script", "replace", str(revised), "--find", "朋友", "--replace", "同学"),
            ]
            for command in commands:
                result = invoke(*command)
                self.assertEqual(result.returncode, 0, result.stderr)
            result = invoke("script", "inspect", str(revised), "--timeline", str(root / "review"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["changed_segments"], 1)
            self.assertEqual(load_script(revised).text_overrides["2"], "你好同学")
            self.assertEqual(dict(load_script(original).text_overrides), {})
            self.assertEqual(source.read_bytes(), before)
            revised_before = revised.read_bytes()
            failed = invoke("script", "replace", str(revised), "--find", "你好", "--replace", "")
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(revised.read_bytes(), revised_before)
            no_change = invoke("script", "replace", str(revised), "--find", "missing", "--replace", "new")
            self.assertEqual(no_change.returncode, 0)
            self.assertEqual(json.loads(no_change.stdout)["replacements"], 0)
            self.assertEqual(revised.read_bytes(), revised_before)

    def test_timeline_cannot_be_used_as_edit_target(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "timeline.json"
            save_timeline(timeline(), path)
            before = path.read_bytes()
            result = invoke("script", "set-text", str(path), "--segment-id", "1", "--text", "new")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(path.read_bytes(), before)

    def test_restore_rename_delete_export_and_base_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            review = root / "review"
            initialize_correction(timeline(), review)
            save_correction(review)
            raw_before = (review / "raw_timeline.json").read_bytes()
            corrected_before = (review / "corrected_timeline.json").read_bytes()
            script = root / "original.json"
            def command(*args):
                result = invoke("script", *map(str, args))
                self.assertEqual(result.returncode, 0, result.stderr)
                return result
            command("create", review, "--id", "o", "--name", "O", "--output", script)
            command("set-text", script, "--segment-id", "1", "--text", "new")
            command("restore", script, "--segment-id", "1")
            self.assertEqual(dict(load_script(script).text_overrides), {})
            target = root / "effective.json"
            command("resolve", script, "--output", target)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["segments"][0]["text"], "你好你好")
            before = target.read_bytes()
            self.assertEqual(invoke("script", "resolve", str(script), "--output", str(target)).returncode, 2)
            self.assertEqual(target.read_bytes(), before)
            for file in ("raw_timeline.json", "corrected_timeline.json", "timeline.json"):
                self.assertEqual(invoke("script", "delete", str(review / file)).returncode, 2)
            self.assertEqual((review / "raw_timeline.json").read_bytes(), raw_before)
            self.assertEqual((review / "corrected_timeline.json").read_bytes(), corrected_before)
            # Metadata-only operations remain possible when the base has moved away.
            (review / "corrected_timeline.json").rename(review / "moved.json")
            command("rename", script, "--name", "新名称")
            self.assertEqual(load_script(script).id, "o")
            self.assertEqual(load_script(script).name, "新名称")
            command("delete", script)
            self.assertFalse(script.exists())
            self.assertTrue((review / "moved.json").is_file())
            self.assertTrue(target.is_file())

    def test_migrate_is_explicit_and_does_not_rewrite_legacy(self):
        from voice_dubbing.models.transcript import timeline_sha256
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            initialize_correction(timeline(), root / "review")
            save_correction(root / "review")
            legacy = root / "old.json"
            legacy.write_text(json.dumps({"id": "old", "name": "Old",
                "source_timeline_sha256": timeline_sha256(timeline()),
                "segments": [{"id": "1", "text": "new"}, {"id": "2", "text": "你好世界"}]}), encoding="utf-8")
            before = legacy.read_bytes()
            self.assertEqual(invoke("script", "inspect", str(legacy)).returncode, 2)
            result = invoke("script", "migrate", str(legacy), "--timeline", str(root / "review"),
                            "--output", str(root / "new.json"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(dict(load_script(root / "new.json").text_overrides), {"1": "new"})
            self.assertEqual(legacy.read_bytes(), before)

    def test_inspect_inherits_new_saved_base_without_rewriting_variant(self):
        from voice_dubbing.correction import edit_correction
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            review = root / "review"
            initialize_correction(timeline(), review)
            save_correction(review)
            script = root / "original.json"
            result = invoke("script", "create", str(review), "--id", "o", "--name", "O", "--output", str(script))
            self.assertEqual(result.returncode, 0, result.stderr)
            before = script.read_bytes()
            edit_correction(review, segment_id="2", text="基础更新")
            save_correction(review)
            result = invoke("script", "inspect", str(script))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["segments"][1]["text"], "基础更新")
            self.assertEqual(script.read_bytes(), before)

    def test_batch_reads_base_once_and_uses_one_snapshot_for_all_combinations(self):
        from voice_dubbing.correction.store import resolve_corrected_timeline
        from voice_dubbing.script_pipeline import run_script_batch
        from test_batch_dubbing import CountingTTS, fake_renderer
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            source.touch()
            review = root / "review"
            initialize_correction(timeline(), review)
            base = save_correction(review)
            original = create_script(base, base_path=review / "corrected_timeline.json", id="o", name="O")
            save_script(original, root / "o.json")
            from voice_dubbing.scripts import copy_script
            save_script(copy_script(original, id="c", name="Copy"), root / "c.json")
            voices = [{"id": "bill", "name": "Bill", "provider": "elevenlabs", "voice_id": "bill-id", "model_id": "model"}]
            jobs = root / "jobs.json"
            jobs.write_text(json.dumps([{"script": id + ".json", "voices": voices} for id in ("o", "c")]), encoding="utf-8")
            seen_bases = []
            def run(job, **kwargs):
                seen_bases.append(job.base_timeline)
                kwargs["renderer"] = fake_renderer
                return run_script_batch(job, **kwargs)
            with patch("voice_dubbing.script_cli.resolve_corrected_timeline", wraps=resolve_corrected_timeline) as reader, \
                    patch("voice_dubbing.script_cli.ElevenLabsTTSProvider", side_effect=lambda **_: CountingTTS()), \
                    patch("voice_dubbing.script_cli.run_script_batch", side_effect=run), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = main(["dub-scripts", str(source), "--timeline", str(review), "--variants-file", str(jobs),
                             "--output-dir", str(root / "out"), "--cache-dir", str(root / "cache")])
            self.assertEqual(code, 0)
            reader.assert_called_once()
            self.assertEqual(len(seen_bases), 1)
            report = json.loads((root / "out" / "script_batch_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["generated_segments"], 2)
            self.assertEqual(report["cache_hits"], 2)
            for id in ("o", "c"):
                exported = root / "out" / "variants" / id / "script_variant.json"
                # Output references its preserved task snapshot, not a mutable external base.
                resolved_base, _ = resolve_corrected_timeline(load_script(exported).base_timeline.path)
                self.assertEqual(resolved_base, base)
                result = invoke("script", "inspect", str(exported))
                self.assertEqual(result.returncode, 0, result.stderr)
