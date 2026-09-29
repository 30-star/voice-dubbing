# 集成到其他程序

## 环境与安装

- Python **3.11+**，已在 Python 3.12 验证。基础包无第三方运行依赖。
- FFmpeg、FFprobe 加入 PATH，或使用 CLI 的 `--ffmpeg` / `--ffprobe` 指定路径。完整媒体测试需要二者；仓库不分发可执行文件。
- VideoCaptioner 在独立环境安装，设置 `VIDEOCAPTIONER_CLI`，仅通过外部进程通信；不复制或 import 其代码。已验证 CLI 版本为 1.4.2。其额外安装参数见根 README。
- Faster-Whisper 备用识别：`pip install -e '.[asr]'`。模型由使用方管理，不在仓库中。
- ElevenLabs 密钥只通过 `ELEVENLABS_API_KEY` 读取。声音可用 `ELEVENLABS_VOICE_ID` 或外部 VoiceProfile 配置。模型、格式、超时、重试可用 `ELEVENLABS_MODEL_ID`、`ELEVENLABS_OUTPUT_FORMAT`、`ELEVENLABS_TIMEOUT`、`ELEVENLABS_MAX_RETRIES` 配置。
- Noiz 密钥使用 `NOIZ_API_KEY`，可用 `voice_dubbing tts-voices --provider noiz` 读取系统及自建声音。模型标识为 `noiz-v1`，独立声音、格式、速度和非密钥参数见 [Noiz 接入](VOICE_DUBBING_NOIZ.md) 与 [Provider Registry](VOICE_DUBBING_TTS_REGISTRY.md)。不在源码或配置保存密钥。

从独立项目根目录安装：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -c "import voice_dubbing; print(voice_dubbing.__file__)"
voice_dubbing --help
```

Private 仓库的使用者必须有 GitHub 访问权限。先通过正常 Git 认证 clone，然后安装本地目录；不要把 GitHub token 写进安装 URL、源码或配置。

## CLI：输入视频得到新视频

调用者应提供**外部可写目录**保存字幕、持久缓存和每次生成的结果，不依赖包安装目录的写权限。以下相对路径以调用者工作目录为基准：

```powershell
# 只识别一次，默认 VideoCaptioner；失败不会自动降级。
voice_dubbing transcribe-video input.mp4 --language zh `
  --output-dir work/subtitles --asr-cache-dir work/cache/asr

# 正常修改只改变文字，保存后供 TTS 使用。
voice_dubbing correction set-text work/subtitles --segment-id 2 --text "修改后的文字"
voice_dubbing correction save work/subtitles

# voices.json 可以只保留 config/voices.json 中的一个声音。
# 使用已经确认的 corrected_timeline，不再 ASR。
voice_dubbing dub-video input.mp4 --timeline work/subtitles `
  --voices-file voices.json --language zh `
  --cache-dir work/cache/tts --output-dir work/generation-001
```

`batch_report.json` 中 `voices[]` 包含 `status`、`video_path`、`audio_path`、缓存统计与错误；每个声音目录保留 MP4、逐句语音和时长报告。CLI stdout 为 JSON，stderr 为错误，等待校正或全部成功退出 0，部分成功 3，失败 2。输出目录必须为空。

再次编辑和 `correction save` 后，改用新的输出目录 `work/generation-002` 调用同一生成命令；保持缓存路径和声音参数一致。未修改的有效缓存复用，改变后的文字仅在未命中缓存时请求 TTS。起止时间、原始字幕不修改，尾音不截断。默认时长匹配接受 ≤150 ms 的误差，明显超长时加速完整音频以保持画面同步，原始 TTS 音频保留。

CLI 默认同时生成 4 句，可用 `--tts-concurrency 1` 至 `8` 调整；每个声音仍独立处理。Noiz 返回明确限流时按提示等待，同一账户的并发请求共享冷却时间，默认最多重试 2 次；余额不足及结果不确定的超时不重试整个生成任务。已有成功句子继续命中缓存。

其他语言的程序可通过参数数组调用，不要拼接 shell 命令。Python 中也可使用 CLI 传输：

```python
import json
import subprocess
import sys

process = subprocess.run(
    [sys.executable, "-m", "voice_dubbing", "dub-video", "input.mp4",
     "--timeline", "work/subtitles", "--voices-file", "voices.json",
     "--language", "zh", "--cache-dir", "work/cache/tts",
     "--output-dir", "work/generation-003"],
    capture_output=True, text=True, encoding="utf-8",
)
if process.returncode not in (0, 3):
    raise RuntimeError(process.stderr)
result = json.loads(process.stdout)
```

## 直接作为 Python 模块调用

`ASRProvider`、`TTSProvider` 仍是可替换的接口。以下生成示例接受已经保存审核的基础字幕，不运行 ASR：

```python
import os
from pathlib import Path
from voice_dubbing.batch_pipeline import run_batch
from voice_dubbing.correction import resolve_tts_timeline
from voice_dubbing.models import BatchDubbingJob, VoiceProfile
from voice_dubbing.tts import CachedTTSProvider, ElevenLabsTTSProvider

timeline, subtitle_source = resolve_tts_timeline(Path("work/subtitles"))
voice = VoiceProfile(
    id="selected", name="我的声音", provider="elevenlabs",
    voice_id=os.environ["ELEVENLABS_VOICE_ID"], model_id="eleven_multilingual_v2",
)

def provider_factory(profile):
    # 唯一的供应商组装点；配音流程只消费 TTSProvider 接口。
    return CachedTTSProvider(
        ElevenLabsTTSProvider(model_id=profile.model_id, output_format="mp3_44100_128"),
        Path("work/cache/tts"),
    )

result = run_batch(
    BatchDubbingJob(Path("input.mp4"), timeline, (voice,)),
    output_dir=Path("work/generation-004"), provider_factory=provider_factory,
    language="zh", subtitle_source=subtitle_source, asr_calls=0,
)
for item in result.voices:
    print(item.status, item.video_path, item.error)
```

`transcribe_video()` 在 `voice_dubbing.pipeline` 中，字幕草稿/保存接口在 `voice_dubbing.correction`，文案变体在 `voice_dubbing.scripts`，单音轨入口在 `voice_dubbing.dubbing_pipeline`，视频合成在 `voice_dubbing.renderer`。宿主程序负责工作目录、线程、取消和展示；不要重新实现核心校正、TTS 或缓存逻辑。

## UI 与验证

Windows WPF UI 与最小桌面程序见 [ui/README.md](../ui/README.md)。它不依赖影匠主程序或调度器。非 Windows 程序可使用 Python API / CLI 构建自己的界面。

```powershell
python -m unittest discover -s tests -v
dotnet build ui/desktop/VoiceDubbing.Desktop.csproj -c Release
```

普通测试不会调用任何真实 TTS 服务。`integration_tests` 默认全部跳过；启用真实验收还需提供自己的外部素材（`VOICE_DUBBING_SOURCE_VIDEO`、`VOICE_DUBBING_TIMELINE` 或 `VOICE_DUBBING_CORRECTED_DIR`）与所需缓存。历史验收针对两句素材，不是任意视频的通用测试，不会为了验收缺失的原句缓存擅自发付费请求；Noiz 单句真实测试也必须显式启用。
