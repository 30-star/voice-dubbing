import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from voice_dubbing.asr import VideoCaptionerASRProvider
from voice_dubbing.asr.cache import cache_key
from voice_dubbing.cli import _parser
from voice_dubbing.errors import ProviderError, ValidationError
from voice_dubbing.pipeline import transcribe_video
from voice_dubbing.timeline.srt import load_srt

SRT = '1\n00:00:00,120 --> 00:00:01,400\n中文字幕\n第二行\n\n2\n00:00:01.300 --> 00:00:02.000\n另一句\n'


class CaptionerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video = self.root / '中文 视频.mp4'
        self.video.write_bytes(b'video')
        self.cli = self.root / 'captioner.exe'
        self.cli.write_bytes(b'launcher')
        self.addCleanup(self.temp.cleanup)

    def provider(self, name='result', **kwargs):
        return VideoCaptionerASRProvider(output_dir=self.root / name, cli_path=self.cli, **kwargs)

    def fake_launch(self, text=SRT, code=0):
        def launch(command, **options):
            if text is not None:
                Path(command[command.index('-o') + 1]).write_text(text, encoding='utf-8')
            options['stdout'].write(b'captured stdout')
            options['stderr'].write(b'captured stderr')
            process = Mock()
            process.wait.return_value = code
            return process
        return launch

    def test_bom_multiline_chinese_gaps_and_overlap(self):
        path = self.root / '字幕.srt'
        path.write_bytes(('\ufeff' + SRT.replace('\n', '\r\n')).encode('utf-8'))
        timeline = load_srt(path)
        self.assertEqual([(s.id, s.start_ms, s.end_ms) for s in timeline.segments], [('1', 120, 1400), ('2', 1300, 2000)])
        self.assertEqual(timeline.segments[0].text, '中文字幕\n第二行')

    def test_bad_srt_is_rejected_without_adjusting_times(self):
        for text in ['', 'not srt', SRT.replace('00:00:01,400', '00:00:00,100'),
                     SRT.replace('00:00:00,120', '00:80:00,120'), SRT.replace('另一句', ' '),
                     SRT.replace('00:00:01.300', '00:00:00.010')]:
            with self.subTest(text=text):
                path = self.root / 'bad.srt'; path.write_text(text, encoding='utf-8')
                with self.assertRaises(ValidationError): load_srt(path)

    def test_command_is_external_isolated_and_preserves_outputs(self):
        provider = self.provider()
        with patch('voice_dubbing.asr.videocaptioner.subprocess.Popen', side_effect=self.fake_launch()) as launch:
            timeline = provider.transcribe_video(self.video, language='zh')
        command = launch.call_args.args[0]
        options = launch.call_args.kwargs
        self.assertEqual(command[:5], [str(self.cli), 'transcribe', str(self.video), '--asr', 'bijian'])
        self.assertEqual(command[command.index('--language') + 1], 'zh')
        self.assertNotIn('shell', options)
        self.assertEqual(options['cwd'], provider.output_dir)
        self.assertEqual(Path(options['env']['TEMP']).parent, provider.output_dir)
        self.assertTrue(Path(options['env']['WIN_PD_OVERRIDE_LOCAL_APPDATA']).is_relative_to(provider.output_dir))
        self.assertTrue(Path(options['env']['USERPROFILE']).is_relative_to(provider.output_dir))
        self.assertTrue((provider.output_dir / 'subtitle.srt').is_file())
        self.assertEqual((provider.output_dir / 'asr_logs/stdout.log').read_bytes(), b'captured stdout')
        self.assertFalse((self.root / 'subtitle.srt').exists())
        self.assertEqual(len(timeline.segments), 2)

    def test_exit_missing_empty_and_malformed_are_explicit_failures(self):
        for i, (text, code) in enumerate([(None, 7), (None, 0), ('', 0), ('bad', 0)]):
            with self.subTest(i=i), patch('voice_dubbing.asr.videocaptioner.subprocess.Popen', side_effect=self.fake_launch(text, code)):
                with self.assertRaisesRegex(ProviderError, 'logs:'):
                    self.provider(str(i)).transcribe_video(self.video)
                self.assertFalse((self.root / str(i) / 'raw_timeline.json').exists())

    def test_timeout_terminates_owned_tree(self):
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired('captioner', .01), 0]
        with patch('voice_dubbing.asr.videocaptioner.subprocess.Popen', return_value=process), \
             patch('voice_dubbing.asr.videocaptioner._kill_tree') as kill:
            with self.assertRaisesRegex(ProviderError, 'timed out'):
                self.provider(timeout=.01).transcribe_video(self.video)
        kill.assert_called_once_with(process)

    @unittest.skipUnless(os.name == 'nt', 'Windows owned process tree verification')
    def test_timeout_really_terminates_parent_and_child(self):
        original = subprocess.Popen
        def launch(_command, **options):
            if len(_command) < 2 or _command[1] != 'transcribe':
                return original(_command, **options)
            return original([sys.executable, '-c',
                "import subprocess,sys,time,os; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(os.getpid(),p.pid,flush=True); time.sleep(60)"], **options)
        with patch('voice_dubbing.asr.videocaptioner.subprocess.Popen', side_effect=launch):
            with self.assertRaisesRegex(ProviderError, 'timed out'):
                self.provider(timeout=1).transcribe_video(self.video)
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        pids = (self.root / 'result/asr_logs/stdout.log').read_text().strip().split()
        self.assertEqual(len(pids), 2)
        for pid in pids:
            handle = kernel.OpenProcess(0x00100000, False, int(pid))
            if handle:
                try: self.assertEqual(kernel.WaitForSingleObject(handle, 2000), 0, f'process {pid} still alive')
                finally: kernel.CloseHandle(handle)

    def test_missing_cli_timeout_and_existing_output_protection(self):
        with self.assertRaisesRegex(ProviderError, 'CLI not found'):
            VideoCaptionerASRProvider(output_dir=self.root, cli_path=self.root / 'missing')
        for timeout in [0, -1, float('nan'), float('inf')]:
            with self.assertRaises(ValidationError): self.provider(timeout=timeout)
        provider = self.provider()
        provider.output_dir.mkdir(); (provider.output_dir / 'subtitle.srt').write_text('old')
        with self.assertRaisesRegex(ProviderError, 'existing'): provider.transcribe_video(self.video)

    def test_video_pipeline_avoids_own_audio_extraction_and_cache_hits(self):
        cache = self.root / 'cache'
        one, two = self.provider('one'), self.provider('two')
        with patch('voice_dubbing.pipeline.probe_video_duration_ms', return_value=3000), \
             patch('voice_dubbing.pipeline.extract_audio', side_effect=AssertionError('audio forbidden')), \
             patch('voice_dubbing.asr.videocaptioner.subprocess.Popen', side_effect=self.fake_launch()) as launch:
            first = transcribe_video(self.video, provider=one, output_dir=one.output_dir, cache_dir=cache, language='zh')
            second = transcribe_video(self.video, provider=two, output_dir=two.output_dir, cache_dir=cache, language='zh')
        self.assertEqual(first, second); self.assertEqual(launch.call_count, 1)
        report = json.loads((two.output_dir / 'asr_report.json').read_text(encoding='utf-8'))
        self.assertEqual((report['asr_calls'], report['audio_extraction_calls'], report['cache_hit']), (0, 0, True))
        self.assertFalse(json.loads((two.output_dir / 'corrected_timeline.json').read_text(encoding='utf-8'))['reviewed'])
        self.assertEqual((one.output_dir / 'subtitle.srt').read_bytes(), (two.output_dir / 'subtitle.srt').read_bytes())
        self.assertFalse(any(cache.rglob('corrected_timeline.json')))

    def test_cache_key_separates_provider_language_parameters_and_video_content(self):
        provider = self.provider()
        key = cache_key(self.video, provider, 'zh')
        self.assertNotEqual(key, cache_key(self.video, provider, 'en'))
        provider.provider_id = 'faster-whisper'
        self.assertNotEqual(key, cache_key(self.video, provider, 'zh'))
        provider.provider_id = 'videocaptioner'; provider.cache_identity['parser_version'] = 2
        self.assertNotEqual(key, cache_key(self.video, provider, 'zh'))
        provider.cache_identity['parser_version'] = 1; self.video.write_bytes(b'new')
        self.assertNotEqual(key, cache_key(self.video, provider, 'zh'))

    def test_corrupt_cache_regenerates_and_failure_never_caches(self):
        cache = self.root / 'cache'
        with patch('voice_dubbing.pipeline.probe_video_duration_ms', return_value=3000), \
             patch('voice_dubbing.asr.videocaptioner.subprocess.Popen', side_effect=self.fake_launch()) as launch:
            provider = self.provider('first')
            transcribe_video(self.video, provider=provider, output_dir=provider.output_dir, cache_dir=cache)
            entry = next(p for p in cache.iterdir() if not p.name.startswith('.'))
            (entry / 'subtitle.srt').write_text('corrupt')
            provider = self.provider('second')
            transcribe_video(self.video, provider=provider, output_dir=provider.output_dir, cache_dir=cache)
            self.assertEqual(launch.call_count, 2)
        with patch('voice_dubbing.pipeline.probe_video_duration_ms', return_value=3000), \
             patch('voice_dubbing.asr.videocaptioner.subprocess.Popen', side_effect=self.fake_launch(None, 9)):
            provider = self.provider('failed')
            with self.assertRaises(ProviderError):
                transcribe_video(self.video, provider=provider, output_dir=provider.output_dir, cache_dir=cache, language='en')
        self.assertEqual(len(list(cache.iterdir())), 1)

    def test_cache_write_failure_is_warning_not_recognition_failure(self):
        with patch('voice_dubbing.pipeline.probe_video_duration_ms', return_value=3000), \
             patch('voice_dubbing.pipeline.publish_cache', return_value='disk full'), \
             patch('voice_dubbing.asr.videocaptioner.subprocess.Popen', side_effect=self.fake_launch()):
            provider = self.provider()
            transcribe_video(self.video, provider=provider, output_dir=provider.output_dir, cache_dir=self.root / 'cache')
        report = json.loads((provider.output_dir / 'asr_report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['warnings'], ['disk full'])

    def test_cli_defaults_and_manual_faster_selection(self):
        args = _parser().parse_args(['transcribe-video', str(self.video)])
        self.assertEqual((args.asr_provider, args.asr_timeout), ('videocaptioner', 600))
        args = _parser().parse_args(['transcribe-video', str(self.video), '--asr-provider', 'faster-whisper'])
        self.assertEqual(args.asr_provider, 'faster-whisper')
