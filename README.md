# voice_dubbing：独立字幕校正与多音色配音

独立 Python 包，附带配音专用 WPF 页面和最小桌面宿主，不包含影匠主程序。默认通过外部卡卡字幕助手 CLI（B 接口）识别视频，Faster-Whisper 保留为本地备用。人工校正并保存基础字幕后，ElevenLabs 可以按多个文案、声音生成音轨和 MP4。Fake TTS 用于离线自动化测试。可以独立运行，也可由影匠智能配音页面调用，未接入自动调度器。

正式页面采用单视频、单声音、单输出：直接编辑已确认的 corrected 字幕，生成时自动保存，后续不重跑 ASR。未修改文字复用持久 TTS 缓存，已经标准化的 PCM WAV 校验后直接复用，避免逐句重复转换。高级 CLI 能力仍保留，页面不暴露多文案组合。详见 [快速修改一句并重新生成](docs/VOICE_DUBBING_RAPID_EDIT.md)。

首次配音默认同时生成 **4 句**。页面“运行环境设置 → TTS 同时生成句数”可设置 1～8 并保存；CLI 的 `dub-video`、`dub-timeline`、`dub-scripts` 支持 `--tts-concurrency 4`。该设置不改变音频缓存键、语速或字幕时间。每个请求使用独立 Adapter，音频按字幕顺序处理；同一任务内重复文本共享缓存，不重复付费生成。发生失败后停止派发新句，等待已经开始的请求结束并保留产物，不自动重试整个任务。Provider 原有的限流重试规则继续生效，遇到限流可降低并发数。

Python 服务调用默认串行以兼容既有自定义 Provider。调用 `run_batch(..., tts_concurrency=4)` 或 `run_script_batch(..., tts_concurrency=4)` 时，`provider_factory` 必须每次返回独立的 Provider 实例。直接调用 `synthesize_timeline(..., tts_concurrency=4)` 时，可用 `tts.concurrent.IsolatedTTSProvider(prototype, factory)` 包装 Adapter 工厂，确保计数、日志和请求状态互不干扰。

## 集成到其他程序

本仓库包含独立 Python 包、158 项离线测试、Provider Adapter、CLI，以及 `ui/` 下的配音专用 WPF 页面和最小桌面宿主；不包含影匠主程序。版本为 **1.1.0**。

- Python **3.11+**；FFmpeg / FFprobe 加入 PATH 或显式指定。
- Noiz 使用环境变量 `NOIZ_API_KEY`，通过官方 API 刷新声音列表；ElevenLabs 使用环境变量 `ELEVENLABS_API_KEY`，声音通过环境变量 `ELEVENLABS_VOICE_ID` 或外部 `config/voices.json` 配置；不提交真实密钥。
- VideoCaptioner 独立安装，通过 `VIDEOCAPTIONER_CLI` 配置；本仓库不包含其源码。Faster-Whisper 可通过 `pip install -e '.[asr]'` 安装可选依赖，模型单独管理。
- 克隆到本机后执行 `python -m pip install -e .`，可 `import voice_dubbing`，也可使用 `voice_dubbing` 或 `python -m voice_dubbing`。
- 基础调用：`transcribe-video` → `correction set-text` / `save` → `dub-video --timeline ... --voices-file ...`；已有已确认字幕直接跳过 ASR。
- 输入视频与缓存、字幕和输出目录由宿主提供。每次生成使用新的空输出目录，MP4 路径通过 stdout JSON 或 `batch_report.json` 读取；修改字幕后使用同一持久缓存，仅未命中的文字重新请求 TTS。
- Windows 桌面 UI：`dotnet run --project ui/desktop/VoiceDubbing.Desktop.csproj -c Release`，需要 .NET 8 SDK；其他 WPF 程序可引用页面项目，见 [UI 集成](ui/README.md)。

完整的 **Python 直接调用示例、CLI 从输入视频到输出视频命令、外部工具配置和返回码**见 [集成指南](docs/INTEGRATION.md)。

仓库不包含媒体、模型、输出、缓存、运行环境、用户设置或真实验收素材。下文保留既有 CLI 功能说明；`docs/` 中的验收数字是原模块的历史记录，真实集成测试必须提供自己的外部素材，并显式启用付费请求。

在本目录运行时间轴校验和离线测试。模型与 JSON 操作只需 Python 3.11+ 标准库；完整测试中的音视频检查还需要 FFmpeg、FFprobe，不调用 ElevenLabs：

```powershell
python -m voice_dubbing --help
python -m voice_dubbing validate-timeline examples/timeline.json
python -m unittest discover -s tests -v
```

默认识别需要 FFmpeg、FFprobe 和独立安装的官方 VideoCaptioner CLI。现有 GUI 1.3.3 不是 CLI；不修改其安装。当前电脑的独立环境位于 `%LOCALAPPDATA%\VideoCaptionerCLI\venv`：

```powershell
python -m venv "$env:LOCALAPPDATA\VideoCaptionerCLI\venv"
& "$env:LOCALAPPDATA\VideoCaptionerCLI\venv\Scripts\python.exe" -m pip install `
  'videocaptioner==1.4.2' 'httpx==0.28.1' 'edge-tts==7.2.8' 'platformdirs==4.12.1'
python -m voice_dubbing transcribe-video 'C:\Videos\sample.mp4' --language zh
```

`httpx` 和 `edge-tts` 补齐官方 1.4.2 CLI 启动时引用、但未完整声明的依赖；影匠仍只调用它的 `transcribe`，不调用其配音。可以用 `--videocaptioner-cli` 或环境变量 `VIDEOCAPTIONER_CLI` 指定其他已验证 CLI。默认超时 600 秒，可用 `--asr-timeout` 修改。只用 bijian 识别，不做 LLM 优化或翻译；B 接口需要联网。

Faster-Whisper 备用方式：

```powershell
python -m pip install -e '.[asr]'
python -m voice_dubbing transcribe-video 'C:\Videos\sample.mp4' --language zh --asr-provider faster-whisper
```

Faster-Whisper 使用 `base`、CPU、INT8。首次使用模型时会下载模型；已有本地模型时可加 `--local-model-only` 避免下载。可以通过 `--model`、`--device`、`--compute-type`、`--language` 更改本地识别设置；通过 `--ffmpeg` 和 `--ffprobe` 指定可执行文件位置。卡卡失败不会自动回退，须显式选择备用方式和新的输出目录。

输出默认位于本模块的 `output\<视频名前40字符>-<随机后缀>\`，包含原始 `raw_timeline.json`、兼容副本 `timeline.json`、初始 `corrected_timeline.json` 和 `asr_report.json`。卡卡额外保留 `subtitle.srt`、`asr_logs`、`asr_tmp`；本地未命中识别保留 `source_audio.wav`。也可用 `--output-dir` 指定空目录。原视频和原始字幕不会被校正命令修改。CLI 返回 `awaiting_correction`，须人工检查并 `correction save` 后才允许配音；失败返回状态码 2。

识别缓存默认位于 `cache/asr`，键包括视频内容、Provider、引擎、语言、模型/识别参数及工具版本。可用 `--asr-cache-dir` 指定目录，`--no-asr-cache` 禁用；这些参数与 TTS 缓存独立。只缓存原始识别，每次命中仍建立独立且未审核的校正字幕。`asr_report.json` 记录 `provider`、`cache_hit`、实际 `asr_calls`；旧 Faster 结果不会被当成卡卡结果。以上参数也适用于需要识别的 `dub-video`，已有校正字幕的配音不初始化 ASR。

时间轴 JSON 使用 UTF-8，结构为 `segments`（片段列表）和 `duration_ms`（完整时长，整数毫秒）。片段字段为 `id`、`start_ms`、`end_ms`、`text`。空时间轴、片段之间的静音和重叠片段均允许；片段按开始时间升序排列。CLI 成功时将校验结果 JSON 写入 stdout，失败时将原因写入 stderr 并返回状态码 2。

Faster-Whisper 适配器遵循 `voice_dubbing.asr.ASRProvider`，对已提取的音频返回 `TranscriptTimeline`。配音流程只消费统一时间轴，并通过 `voice_dubbing.tts.TTSProvider` 调用 TTS。

## ASR 字幕校正

字幕校正属于基础识别结果，不是 ScriptVariant。每句保存 `id`、原始起止时间、`original_text`、已保存 `text`，并按两段文字是否相同输出 `edited`。顶层保存 raw 指纹和 `reviewed` 状态；新识别结果默认未审核。

编辑只写 `correction_draft.json`，显式保存后才更新 `corrected_timeline.json`。撤销丢弃上次保存以来的全部草稿。恢复原文同样先修改草稿，保存后才生效。所有操作都保留原始文件、片段数量、ID、顺序和时间。

```powershell
python -m voice_dubbing transcribe-video 'C:\Videos\sample.mp4' `
  --language zh --local-model-only --output-dir 'output\review'
python -m voice_dubbing correction inspect 'output\review'
python -m voice_dubbing correction set-text 'output\review' `
  --segment-id 2 --text '那你这辈子基本就定型了'
python -m voice_dubbing correction save 'output\review'
```

其他操作：

```powershell
python -m voice_dubbing correction discard 'output\review'
python -m voice_dubbing correction restore 'output\review' --segment-id 2
python -m voice_dubbing correction restore-all 'output\review'
# 恢复操作仍须 save；inspect 同时显示原文、已保存文字和草稿文字。
```

历史普通 Timeline 可导入新的空目录，完全不需要再运行 ASR：

```powershell
python -m voice_dubbing correction init 'output\旧任务\timeline.json' --output-dir 'output\legacy-review'
python -m voice_dubbing correction inspect 'output\legacy-review'
python -m voice_dubbing correction save 'output\legacy-review'
```

`save` 即使没有文字修改，也会确认审核状态。校正 JSON 使用严格字段校验，拒绝未知 ID、空白文字、伪造的修改标记、时间变化、指纹不符及损坏文件。保存采用临时文件与原子替换；失败保留旧内容和草稿。草稿带有其所基于的保存版本指纹，过期草稿不能覆盖其他编辑者保存的结果。

配音 CLI 接受字幕目录或其中的 `corrected_timeline.json`；即使传入同目录的 `raw_timeline.json` 或 `timeline.json`，仍读取 corrected。没有校正文件或尚未审核时，在第一次 TTS 请求前报错，不回退到原文。不读取草稿；已保存结果不受未保存改动影响。

## 单音色音轨测试

显式选择 `fake` 会为每句生成测试音（**不是人声**），可验证字幕定位、重叠混音、超长提示和完整 WAV 输出：

```powershell
python -m voice_dubbing dub-timeline 'output\review' --provider fake --voice fake-default
```

命令在独立的新输出目录下生成 `segments/000001/speech.wav` 等逐句音频、`dubbed_audio.wav`、`dubbing_report.json`、`dubbing_report.csv` 和 `logs/`。可通过 `--output-dir` 指定**空目录**，通过 `--ffmpeg` 指定 FFmpeg。报告记录目标时长、实际测量的音频时长、带符号的 `overflow_ms = actual_duration_ms - target_duration_ms` 及重叠警告；Fake 标明 `is_mock=true`。超长语音从原字幕起点播放到结束；重叠区混音，末句若超过视频时长则延长完整音轨。不做时间拉伸或裁切。Fake 的时长仅用于测试流程，不能代表真实人声语速。

时长匹配采用 150 ms 容差：默认以字幕结束与下一句开始中较早的时间为播放窗口；超出超过 150 ms 时，用 FFmpeg `atempo` 加速完整音频并实测输出，避免明显人声重叠。容差内保持自然语速，末句轻微超出仍保留尾音。所有原始字幕时间、文本与 TTS 音频保持不变，不重新请求 TTS。

JSON/CSV 保留 `actual_duration_ms`、`overflow_ms` 和 `overlap_with_next_ms` 的原始 TTS 含义，新增 `playback_audio_path`、`playback_duration_ms`、`speed_factor`、`duration_adjusted`、`playback_overlap_with_next_ms` 描述最终播放结果。处理后的 `matched.wav` 只存于本次任务，不写入 TTS 缓存；同一原音频在不同窗口下重新计算匹配。Python `synthesize_timeline(..., match_duration=False)` 可用于原始重叠行为的对比测试。大于 1.5 倍加速会记录听感检查提示；同时起点的字幕无法顺序对齐，会在 TTS 前明确拒绝。

加速因子超过 2 时拆分为多级 `atempo`，依据 [FFmpeg 官方说明](https://ffmpeg.org/ffmpeg-filters.html#atempo)。不使用逐句截断、改写文本或重采样升调；按完整处理结果进行混音和尾音覆盖。

## ElevenLabs 真实语音

在运行命令的进程环境中设置 `ELEVENLABS_API_KEY`；不要把密钥写入命令、代码、日志或提交文件。单次任务可用 `--voice <voice_id>` 指定一个声音，也可用 `--voice auto` 从预置声音中选一个，优先有中文语音样本的声音。免费账户可能列出但无法通过 API 使用声音库声音；如需指定此类声音，必须具备相应账户权限。声音一旦选定，所有字幕都使用它。

```powershell
python -m voice_dubbing dub-timeline 'output\review' --provider elevenlabs --voice auto
```

`--model-id` 默认 `eleven_multilingual_v2`，`--output-format` 默认 `mp3_44100_128`，`--timeout` 默认 60 秒，`--max-retries` 默认 2。可分别通过 `ELEVENLABS_MODEL_ID`、`ELEVENLABS_OUTPUT_FORMAT`、`ELEVENLABS_TIMEOUT`、`ELEVENLABS_MAX_RETRIES` 设置默认值；命令行参数优先。`ELEVENLABS_VOICE_ID` 可替代 `--voice`。首版支持 MP3 和 PCM 输出格式，非法格式会在请求前报错。部分高采样率格式受账户等级限制，由 ElevenLabs 返回明确错误。

每句原始响应保存在任务自己的 `segments/000001/speech.mp3` 或 `speech.pcm`，同目录的 `speech.wav` 为测量用音频。报告中的 `audio_path` 指向后者，`voice_name`、`model_id` 和 `output_format` 记录本次实际配置；`is_mock=false`。网络请求日志只记录状态、次数及请求 ID，并隐藏密钥。没有可用密钥时命令立即报错，绝不会生成 Fake 音频。

普通离线测试不会请求 ElevenLabs。仅在准备进行真实付费验收时显式运行：

```powershell
$env:VOICE_DUBBING_RUN_INTEGRATION = '1'
python -m unittest discover -s integration_tests -v
```

该测试使用上述两句的真实 `timeline.json`，在 `output` 下创建独立任务目录并保留真实音频与报告。若本机有缓存的 Faster-Whisper `base` 模型，还会生成逐句 `tts_asr_check.json` 辅助核对发音；ASR 结果不代替人工试听。没有 `ELEVENLABS_API_KEY` 时标记跳过。

## 一个视频输出多个声音版本

批量命令默认只识别视频一次，然后暂停等待校正。使用 `--timeline` 指定已审核字幕目录时，不提取音频、不调用 ASR，直接为每个声音生成音频及 MP4：

```powershell
python -m voice_dubbing dub-video 'C:\Videos\sample.mp4' `
  --timeline 'output\review' `
  --voices 'pqHfZKP75CvOlQylNhV4,EXAVITQu4vr4xnSDxMaL' `
  --language zh
```

`--voices` 为逗号分隔的显式 ElevenLabs voice ID，不接受 `auto`。如需声音名称、目录别名或每个声音不同的模型，改用 `--voices-file voices.json`，其内容是 JSON 数组：

```json
[
  {"id": "bill", "name": "Bill", "provider": "elevenlabs", "voice_id": "pqHfZKP75CvOlQylNhV4", "model_id": "eleven_multilingual_v2"},
  {"id": "sarah", "name": "Sarah", "provider": "elevenlabs", "voice_id": "EXAVITQu4vr4xnSDxMaL", "model_id": "eleven_multilingual_v2"}
]
```

不带 `--timeline` 时返回 `awaiting_correction`，不会初始化 TTS Adapter，也不需要密钥；保存基础字幕后，用上面的命令继续配音。自动化场景可显式加 `--accept-asr` 接受新识别的原文并立即配音，它与 `--timeline` 互斥。`--asr-model` 配置 Faster-Whisper；`--model-id` 设置 `--voices` 的默认 TTS 模型。格式、超时和重试参数与单声音命令相同。TTS 密钥只读取对应 Provider 的环境变量。声音按顺序处理，每个声音内默认并发生成 4 句；一个声音失败仍继续下一个。

默认在模块 `output/<任务名>/` 创建唯一目录。识别阶段保留字幕校正文件组和源音频；消费已审核字幕时在新的输出目录保存实际配音 Timeline 快照、批量报告及各声音的产物。`batch_report.json` 记录 raw/corrected 指纹、来源、修改标记、ASR 次数、声音状态、路径、警告、缓存命中、生成数和真实 TTS HTTP 请求次数。逐声音 JSON/CSV 的 `subtitle_edited` 表示基础字幕校正标记。全部成功退出码为 `0`，部分成功为 `3`，公共阶段或所有声音失败为 `2`；等待校正正常返回 `0`。指定 `--output-dir` 时目录必须为空。

批量任务默认启用跨任务持久缓存，位置为模块 `cache/tts`；可用 `--cache-dir` 更换，或 `--no-cache` 关闭。缓存键包含原文、Provider、voice ID、模型、输出格式、语言和有效 TTS 参数。命中后仍会把原始语音及 WAV 复制到本次句子目录。单声音 `dub-timeline` 默认行为不变，可用 `--cache-dir` 显式启用同一缓存。缓存不会保存 API Key。

视频合成保留原画面并替换音轨；若轻微超出的尾音长于画面，则冻结末帧直至完整尾音结束。逐句音频从原 `start_ms` 播放，误差与重叠 `≤150ms` 不触发变速；明显超长时使用上述时长匹配。

真实批量验收单独启用，会产生付费 ElevenLabs 请求：

```powershell
$env:VOICE_DUBBING_RUN_BATCH_INTEGRATION = '1'
python -m unittest integration_tests.test_batch_live -v
```

## 稀疏文案版本 ScriptVariant（schema_version=2）

文案版本建立在已保存、已审核的 `corrected_timeline.json` 上，与 ASR 原始字幕、人工校正层分开。版本只保存 `text_overrides`，不复制 Timeline，不允许改变 ID、顺序、时间和总时长。未覆盖的句子自动继承基础字幕后续保存的校正文字；覆盖项保持不变。解析、编辑或配音不会修改 raw、corrected 或其他版本。

```json
{
  "schema_version": 2,
  "id": "variant_a",
  "name": "版本A",
  "base_timeline": {
    "path": "../subtitles/corrected_timeline.json",
    "raw_timeline_sha256": "<原始ASR的SHA-256>",
    "structure_sha256": "<ID、顺序、时间、总时长的SHA-256>"
  },
  "text_overrides": {"1": "连这本好书都舍不得买"},
  "created_at": "2026-09-28T00:00:00Z",
  "updated_at": "2026-09-28T00:01:00Z"
}
```

同盘基础引用相对于版本文件目录保存；跨盘使用绝对路径。复制到其他目录会重新计算相对引用。原始 ASR 身份或时间结构不一致时拒绝绑定；仅校正文字变化不会使版本失效。原版覆盖表为空；设置为当前基础文字、或 `restore`，均删除对应覆盖。

```powershell
# 在仓库根目录下运行。
# 使用现有已审核字幕，不重新运行 ASR。
python -m voice_dubbing script create 'output\source' `
  --id original --name 原版 --output 'scripts\original.json'
python -m voice_dubbing script copy 'scripts\original.json' `
  --id variant_a --name 版本A --output 'scripts\variant_a.json'
python -m voice_dubbing script set-text 'scripts\variant_a.json' `
  --segment-id 1 --text '连这本好书都舍不得买'
python -m voice_dubbing script copy 'scripts\original.json' `
  --id variant_b --name 版本B --output 'scripts\variant_b.json'
python -m voice_dubbing script set-text 'scripts\variant_b.json' `
  --segment-id 2 --text '那你这辈子基本就这样了'
python -m voice_dubbing script inspect 'scripts\variant_a.json'
python -m voice_dubbing script replace 'scripts\variant_a.json' --find '好书' --replace '书'
python -m voice_dubbing script restore 'scripts\variant_a.json' --segment-id 1
python -m voice_dubbing script rename 'scripts\variant_a.json' --name 新名称
python -m voice_dubbing script resolve 'scripts\variant_a.json' --output 'scripts\effective_timeline.json'
python -m voice_dubbing script delete 'scripts\variant_a.json'
```

创建、复制、导出和迁移禁止覆盖已有目标；复制必须使用新 ID，并产生新创建时间。编辑直接原子保存，失败保留旧文件；实际变化才更新 `updated_at`。替换作用于当前有效文字，区分大小写、按字面匹配全部出现位置；零命中不重写。未知片段、空查找词、空白文字、重复 JSON 键、额外时间字段均拒绝。重命名只改变展示名称，删除只删除指定版本文件，二者不要求基础仍可访问。编辑、复制、检查及解析支持可选 `--timeline` 显式定位同一基础。

旧快照格式不自动改写，使用显式迁移另存：

```powershell
python -m voice_dubbing script migrate 'scripts\旧版本.json' `
  --timeline 'output\source' --output 'scripts\迁移版本.json'
```

迁移要求旧指纹与当前已审核基础有效 Timeline 完全一致，并验证完整片段 ID 与顺序，之后计算稀疏覆盖。无法确认旧基础时需重新创建版本，不能猜测差异。

### 版本 × 声音批量配音

`jobs.json` 是数组，每项为一个版本及完整声音列表。脚本相对路径以清单目录为基准；各版本可选择不同声音组合。例如在 `scripts/jobs.json` 配置原版、A、B，三项均使用以下声音列表：

```json
[
  {
    "script": "original.json",
    "voices": [
      {"id": "bill", "name": "Bill", "provider": "elevenlabs", "voice_id": "pqHfZKP75CvOlQylNhV4", "model_id": "eleven_multilingual_v2"},
      {"id": "sarah", "name": "Sarah", "provider": "elevenlabs", "voice_id": "EXAVITQu4vr4xnSDxMaL", "model_id": "eleven_multilingual_v2"}
    ]
  }
]
```

```powershell
python -m voice_dubbing dub-scripts 'C:\Videos\sample.mp4' `
  --timeline 'output\source' --variants-file 'scripts\jobs.json' --language zh
```

任务只读取一次基础快照，验证全部版本和声音后，按版本、声音顺序执行；每个组合内默认并发生成 4 句，最终按原字幕顺序合成。不调用 ASR 或音频提取。一个组合失败仍继续其余组合。密钥只读取环境变量；模型由 VoiceProfile 指定，其他 TTS、FFmpeg、输出和缓存选项沿用现有入口。全部成功返回 `0`，部分成功 `3`，公共阶段或全部失败 `2`。

```text
output/<task>/
├── timeline.json                      # 校正后基础的有效文字快照
├── base/
│   ├── raw_timeline.json
│   └── corrected_timeline.json
├── script_batch_report.json
└── variants/<variant.id>/
    ├── script_variant.json            # 引用本次任务的基础快照
    ├── effective_timeline.json
    └── <voice.id>/                    # segments、WAV、JSON/CSV、日志、MP4
```

MP4 名称为 `<源视频名>_<variant.id>_<voice.id>.mp4`。报告保存版本及声音的 ID/名称、实际基础与有效 Timeline 指纹、逐句 `text_source=base/override`、缓存命中、生成数、HTTP 请求数和时长。缓存键完全不变，不包含版本、路径、名称、时间戳或基础指纹；相同有效文字和声音配置继续命中。两句、三版本、两声音，原版缓存齐全而两个新句未缓存时：命中 8 条，新生成 4 条；重复运行复用新增缓存。

继续接受 ≤150 ms 的偏差；明显超出使用上述时长匹配，保留原始人声并记录实际加速比例，不改写文本。画面中已有字幕不会随新文案更新。

### 本阶段真实验收

独立测试使用已审核基础，不运行 ASR 或提取音频。检查基础缓存后，真实请求仅允许用于上述 A/B 新句；基础缓存缺失即停止。六份 MP4 完整解码检查和 raw/corrected 字节不变检查包含在验收内，听感由用户试听确认。

```powershell
$env:VOICE_DUBBING_RUN_SPARSE_SCRIPT_INTEGRATION = '1'
python -m unittest integration_tests.test_sparse_scripts_live -v
Remove-Item Env:VOICE_DUBBING_RUN_SPARSE_SCRIPT_INTEGRATION
```

普通离线测试不会请求 API。真实验收重复运行时正常使用缓存，不强制再次付费。

## 真实字幕校正验收

单独运行本阶段验收，预计产生两次付费请求。测试先识别一次，校正并保存第二句，然后使用 Bill、Sarah 配音。独立缓存只复制已验证的原句条目，未修改句命中缓存，修改句各生成一次。现有持久缓存完全不变；原句缓存缺失时停止，不重新请求原句。

```powershell
$env:VOICE_DUBBING_RUN_CORRECTION_INTEGRATION = '1'
python -m unittest integration_tests.test_correction_live -v
Remove-Item Env:VOICE_DUBBING_RUN_CORRECTION_INTEGRATION
```

产物保存在 `output/correction-acceptance-<ID>/`：`subtitles/` 保存 raw、corrected 和原始兼容副本；`results/` 保存两个 MP4、音轨及 JSON/CSV；`acceptance_check.json` 记录 ASR 初次 1 次、校正后 0 次、文件指纹、缓存及逐句时长检查。原视频画面字幕不随校正文字更新；本阶段不修改画面字幕。

## 配音服务选择与扩展

正式页面现在有「配音服务」和「配音声音」两个下拉框。ElevenLabs / Bill 默认兼容；Noiz 已接入真实 Adapter，配置 `NOIZ_API_KEY` 后自动读取其声音列表，也可使用“刷新声音”。火山、MiniMax、SiliconFlow 仍为预留项。未配置或列表失败时禁止生成，不会回退或继续使用旧服务的声音。

`python -m voice_dubbing tts-providers` 离线列出状态。通过 `--tts-config examples/tts-providers.json` 读取各 Provider 的非密钥参数，密钥只来自环境变量。新的缓存键包含服务、模型、声音、文字、格式、语言、速度和音频参数；默认速度保留对有效旧缓存的只读兼容。

Noiz 的参数、使用命令和独立真实测试说明见 [Noiz 接入](docs/VOICE_DUBBING_NOIZ.md)。Noiz 使用 `noiz-v1` 作为内部模型身份，不向官方接口发送未支持的 model_id。中文传 `target_lang=zh`；支持 WAV/MP3、语速、质量及情绪参数。默认非流式，不提供克隆功能。普通测试不请求真实 API，真实验证必须显式启用。

下一家 Adapter 的接入接口、文件和边界见 [Provider Registry](docs/VOICE_DUBBING_TTS_REGISTRY.md)。已有字幕、缓存、时长匹配和视频流程继续复用，不新增 UI 高级组合。
