# 卡卡字幕助手默认 ASR 接入

智能配音新项目默认使用“卡卡字幕助手（推荐，需联网）”，底层为官方独立 CLI 1.4.2 的 bijian/B 接口。已安装的卡卡 GUI 1.3.3 保留。影匠不复制、import、发布外部程序源码，也不调用其优化、TTS 或 renderer。

## 使用

1. 打开智能配音，添加视频，选择字幕识别方式。运行环境设置可配置卡卡 CLI 路径与超时。
2. 点击识别字幕，检查、编辑字幕，选择 Bill 或 Sarah 中的一个声音，点击「生成新配音视频」。生成时自动保存校正字幕，详见 [单输出第一版](VOICE_DUBBING_SINGLE_OUTPUT.md)。
3. 识别失败可选择 Faster-Whisper 重试；失败目录和日志保留。成功项目的字幕来源固定，添加视频建立独立项目；第一版不暴露任务分叉或文案变体。

CLI 默认入口及备用入口：

```powershell
Set-Location '.'
python -m voice_dubbing transcribe-video 'C:\Videos\sample.mp4' --language zh
python -m voice_dubbing transcribe-video 'C:\Videos\sample.mp4' --asr-provider faster-whisper --local-model-only --language zh
```

## 隔离与数据

新 Adapter 遵循原音频 ASR 接口，并实现可选 `VideoASRProvider` 能力。编排层按能力传入原视频或提取的音频，后续只获得 `TranscriptTimeline`。SRT 解析由影匠自行实现，不调整文字和时间。

raw/corrected、ScriptVariant、TTS 缓存键、150 ms 容差、混音、视频合成和任务失败隔离保持原行为。ASR 缓存独立为 `cache/asr`，不缓存人工校正；旧会话不会自动重识别或迁入卡卡缓存。

每次识别使用新目录；日志、字幕和外部临时文件均在任务内。子进程使用参数列表、隐藏窗口、明确超时和进程树终止，不静默重试或自动降级。取消识别后可以重新选择 Provider，不覆盖原字幕。

## 验证入口

```powershell
python -m unittest discover -s tests -v
dotnet artifacts/voice-dubbing-release/VideoCraftStudio.CoreChecks.dll --voice-dubbing-check
# 显式真实网络验收；会消耗 ElevenLabs 额度，重复执行正常复用缓存。
$env:VOICE_DUBBING_RUN_UI_INTEGRATION = '1'
dotnet tests/GaoxiaoVideo.CoreChecks/bin/Release/net8.0-windows10.0.17763.0/VideoCraftStudio.CoreChecks.dll --voice-dubbing-single-acceptance
```

此前卡卡接入验收使用 44.77 秒视频，校正第 10 句“常见于”→“常见鱼”，变体仅将第 1 句末尾增加“元”；原版及变体各用 Bill/Sarah，共四份视频，历史产物继续保留。当前单输出验收复用已校正字幕，修改第一句并生成一个 Bill 视频，不重跑 ASR；旧 UI 验收参数作为单输出验收的兼容别名。完整尾音保留，明显重叠仅报告警告，听感由用户试听确认。
