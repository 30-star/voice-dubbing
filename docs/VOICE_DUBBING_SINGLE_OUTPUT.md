# 智能配音第一版：单视频、单声音、单输出

当前正式页面流程：添加视频 → 识别字幕 → 检查/修改字幕 → 选择一个声音 → 生成新配音视频 → 播放结果。

## 使用

打开 Release 目录的 VideoCraftStudio.exe，在侧栏选择「智能配音」。默认识别方式为卡卡字幕助手，保留 Faster-Whisper 本地备用选择。成功识别后不再重复识别；添加另一个视频建立独立项目。

字幕表的起止时间只读，文字可以直接编辑；修改后显示「已修改／未保存」。可以恢复所选句的 ASR 原文、恢复全部或放弃未保存修改。原始 raw_timeline 不被覆盖。

选择 Bill 或 Sarah，只能选一个。首次默认 Bill，后续记住选择。点击「生成新配音视频」会提交正在编辑的单元格、等待编辑完成、保存 corrected_timeline 并确认审核，然后生成。无需额外点击保存；也可以提前点击「保存字幕」。非法文本、编辑失败或保存失败均会阻止 TTS。

进度显示实际阶段：保存字幕、生成配音音轨、合成并检查视频、完成；使用不确定进度条，没有估算百分比。完成后显示声音与完整路径，提供播放、打开目录和查看报告。字幕再次修改时提示上次结果已过期，需要重新生成。

生成期间输入锁定，页面切换不会停止任务。停止或关闭应用终止本功能拥有的进程树，保留日志与产物，不自动重试。重新打开恢复最近单输出项目，不自动识别或生成。

文案变体、多声音、多文案组合及任务分叉不在本版页面暴露。已有 Python 核心、CLI、高级服务接口、缓存和历史文件仍然保留。旧项目只恢复 corrected 基础字幕，隐藏变体不参与本次生成。

## 服务与核心边界

- `VoiceDubbingSingleService.cs` 为现有 C# 服务增加 `GenerateSingleAsync`、`ReadSingleGenerationAsync`。只调用现有 `dub-video --timeline <字幕目录> --voices-file <单声音配置>`，生成阶段 ASR 和识别音频提取为零。
- 独立 `DubbingSingleGeneration` 保存状态、阶段、声音、路径、错误、警告和缓存计数。会话新增可选 `LastSingleGeneration`，保留原批量索引字段；运行配置新增 `LastVoiceId`，不保存密钥。
- 配音前不创建、加载或解析 ScriptVariant。校正保存移除隐式创建原版的副作用；高级调用方可显式使用仍保留的版本服务。
- 连接服务每秒读取现有 batch_report.json，并依据正式音轨文件区分音轨与视频阶段。页面/ViewModel 不启动进程、不读写核心 JSON、不请求供应商 API。
- 单输出页面接入阶段未修改 Python 核心。后续 [快速修改重生成](VOICE_DUBBING_RAPID_EDIT.md) 仅增加标准 PCM WAV 的复用路径；既有 TTS 缓存键、150 ms 容差、混音和视频 renderer 保持不变。明显超长只警告，保持完整尾音，不变速或裁剪。

## 验证与真实产物

离线 WPF 检查：

```powershell
dotnet build tests/GaoxiaoVideo.CoreChecks/GaoxiaoVideo.CoreChecks.csproj -c Release
dotnet tests/GaoxiaoVideo.CoreChecks/bin/Release/net8.0-windows10.0.17763.0/VideoCraftStudio.CoreChecks.dll --voice-dubbing-check
./scripts/Test-Core.ps1
```

检查覆盖实际单元格编辑、生成按钮提交未完成编辑、自动保存、保存失败门禁、空白文字拒绝、恢复/撤销、单选声音、结果过期提示、页面离开后继续、取消、单报告错误终止子进程、恢复及明暗/最小布局。高级 CLI/服务的版本继承、批量失败隔离与 0/3/2 返回码检查仍保留。

真实网络验收必须显式启用 `VOICE_DUBBING_RUN_UI_INTEGRATION=1`，运行 `--voice-dubbing-single-acceptance`。使用已验收的 44.77 秒视频及 corrected 字幕，先验证全部未修改句缓存；缓存缺失立即停止。经实际 WPF 编辑和生成按钮，将第一句改为「这本书要是卖三四十块」，选择 Bill，不单独点击保存。

本次真实产物在 `tools/voice_dubbing/output/single-ui-01122c26/`：

- 生成一次、输出一个 MP4；重新 ASR 0 次、Script 命令 0 次。
- 21 句：20 句缓存命中，修改句新生成 1 次、真实 HTTP 请求 1 次。
- raw 文件字节和全部 ID/时间字段保持不变，corrected 自动保存成功。
- 原时间轴 44769 ms；完整 WAV 45352.167 ms；MP4 45400 ms。尾音完整保留。
- 修改句目标 1460 ms，实际 2415 ms，overflow +955 ms；继续按原规则记录超长/重叠警告。本次不改变时长算法。
- MP4 完整解码通过；页面播放按钮通过 WPF 媒体打开和位置推进验证。主观听感由用户试听确认。
- 截图、acceptance.json 在验收目录根部；逐句 JSON/CSV、完整 WAV、MP4 和 batch_report.json 在 `project/generations/e4ee28eea149/`。

完成此阶段后停止，不接任务调度器。

## 单输出页面接入阶段测试结果

- Python 核心：109/109 通过，失败 0、跳过 0。
- WPF/CLI 专项：通过，含未提交单元格直接生成、保存失败门禁、取消以及报告异常时停止所属子进程。
- `scripts/Test-Core.ps1` 完整执行：111/111 通过，失败 0、跳过 0；架构检查通过。
- 标准 Release 最终构建：0 警告、0 错误，原 Release 目录已更新。
- 真实验收：一个 Bill MP4，ASR 0 次、20 条缓存命中、1 条新生成、1 次 HTTP；完整解码和播放检查通过。

结构化测试结果见验收目录 `test_results.json`，页面截图已检查浅色、深色及最小窗口滚动后操作区域。工作区原有改动、缓存和历史视频保留，未新增提交或接入任务调度器。
