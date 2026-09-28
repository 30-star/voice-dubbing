import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from voice_dubbing.cli import _batch_profiles, _parser


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def invoke(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "voice_dubbing", *args],
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


class CLITests(unittest.TestCase):
    def test_help(self):
        result = invoke("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("validate-timeline", result.stdout)

    def test_example_timeline(self):
        result = invoke("validate-timeline", "examples/timeline.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {"valid": True, "segments": 1, "duration_ms": 32000})
        self.assertEqual(result.stderr, "")

    def test_unicode_and_space_in_path(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "中文 时间轴.json"
            path.write_text('{"segments": [], "duration_ms": 0}', encoding="utf-8")
            result = invoke("validate-timeline", str(path))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["segments"], 0)

    def test_invalid_input_or_arguments_exits_two(self):
        for args in [("validate-timeline", "does-not-exist.json"),
                     ("validate-timeline",), ("transcribe-video", "missing.mp4"),
                     ("dub-timeline", "missing.json", "--provider", "fake"),
                     ("unknown-command",)]:
            with self.subTest(args=args):
                result = invoke(*args)
                self.assertEqual(result.returncode, 2)
                self.assertTrue(result.stderr)
                self.assertEqual(result.stdout, "")

    def test_dub_timeline_requires_explicit_provider(self):
        result = invoke("dub-timeline", "examples/timeline.json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--provider", result.stderr)

    def test_dub_video_rejects_bad_voice_list_before_asr(self):
        result = invoke("dub-video", "missing.mp4", "--voices", "bill,bill")
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate", result.stderr)
        result = invoke("dub-video", "missing.mp4", "--voices", "auto")
        self.assertEqual(result.returncode, 2)
        self.assertIn("explicit", result.stderr)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "voices.json"
            path.write_text('{"voices": []}', encoding="utf-8")
            result = invoke("dub-video", "missing.mp4", "--voices-file", str(path))
            self.assertEqual(result.returncode, 2)
            self.assertIn("JSON array", result.stderr)

    def test_voices_file_preserves_alias_and_model(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "声音配置.json"
            path.write_text(json.dumps([{
                "id": "bill", "name": "Bill", "provider": "elevenlabs",
                "voice_id": "voice-bill", "model_id": "eleven_multilingual_v2",
            }], ensure_ascii=False), encoding="utf-8")
            args = _parser().parse_args(["dub-video", "video.mp4", "--voices-file", str(path)])
            profile, = _batch_profiles(args)
            self.assertEqual((profile.id, profile.name, profile.voice_id, profile.model_id),
                             ("bill", "Bill", "voice-bill", "eleven_multilingual_v2"))


if __name__ == "__main__":
    unittest.main()
