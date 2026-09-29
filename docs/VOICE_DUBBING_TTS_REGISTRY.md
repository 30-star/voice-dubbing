# 配音服务 Registry

智能配音现在有「配音服务」和「配音声音」两个选择。默认仍为 ElevenLabs / Bill。切换服务会清空旧声音并重新过滤声音配置；模型与 voice_id 都来自当前服务的 VoiceProfile。未配置密钥或未实现 Adapter 时禁止生成，字幕编辑和识别仍可用。

当前唯一真实 Adapter 是 ElevenLabs。火山引擎、MiniMax、SiliconFlow 只注册展示信息和环境变量前缀，显示未配置/尚未接入，不调用真实接口；Fake 只用于显式 CLI 和自动化测试，不显示在正式 UI，不自动降级。

## 边界与配置

- `tts/registry.py` 集中提供 `ProviderDescriptor`、`ProviderRegistry.register()`、`configuration()`、`create()` 和离线 `list()`。CLI 编排不构造具体供应商 Adapter。
- 接口保持 `TTSProvider.synthesize(SpeechRequest, *, output_dir) -> SpeechAudio`。`SpeechRequest` 仍包含片段 ID、文字、声音和语言；供应商 HTTP、认证、参数能力及音频解码由 Adapter 负责。
- `config/tts.py` 的 `TTSConfiguration` 保存 provider、model_id、output_format、timeout、max_retries、speed、parameters 和 FFmpeg 路径；不保存密钥。禁止配置文件或嵌套音频参数包含凭据字段。
- 参数优先级：显式 CLI / VoiceProfile 模型、当前 Provider 的 JSON 配置、当前 Provider 的环境变量、Registry 默认值。不会读取其他 Provider 的模型、格式或声音变量。
- ElevenLabs 仍使用 `ELEVENLABS_API_KEY`。其他前缀目前是接入占位约定：`VOLCENGINE_*`、`MINIMAX_*`、`SILICONFLOW_*`，具体鉴权方式在新增 Adapter 时确认。
- UI 的环境设置可填写外部 TTS 配置文件。示例：`tools/voice_dubbing/examples/tts-providers.json`。声音仍在 `resources/voice_dubbing_voices.json`，无需在 UI 写死供应商声音 ID。
- 原 ElevenLabs 请求体保持不变。当前 Adapter 只接受默认 speed=1 和空额外参数；不支持的参数会明确拒绝，不假装已应用，也不执行音频变速。

```powershell
python -m voice_dubbing tts-providers
python -m voice_dubbing dub-video input.mp4 --timeline subtitles `
  --voices-file voices.json --tts-config examples/tts-providers.json
```

配置读取、Provider 创建与注册发生在首次 TTS 前。`tts-providers` 无网络请求，只输出名称、实现/配置状态和环境变量名称，不输出密钥。

## 缓存兼容

新缓存键 v2 包含 provider、model_id、voice_id、text、output_format、language、显式 speed 及所有有效音频参数。缓存包装器读取 Registry 传递的已生效配置，深拷贝参数，拒绝嵌套凭据。

默认 speed=1 可按相同 Provider、模型、声音、文字、格式、语言、参数的旧键读取已验证 v1 缓存；非默认速度不读取旧键。旧条目不删除、不重写；损坏条目仍按未命中处理，失败不缓存。未修改句继续复用，150 ms 容差与完整尾音不变。

## 增加下一家服务

1. 新增 `voice_dubbing/tts/<provider>.py`，实现 `TTSProvider`；供应商 HTTP 请求只放在该 Adapter。保证 task-local 原始音频和测量 WAV、实际时长、清晰异常、脱敏及超时/重试。
2. 提供 `create_from_configuration(config: TTSConfiguration)`，验证并实际使用支持的 model/format/speed/parameters，不能悄悄忽略影响音频的参数。
3. 在 `default_registry()` 中把对应的 `None` 替换为工厂；设置正确的环境变量前缀、默认模型与格式。如果需要不同于 API_KEY 的多字段认证，扩展 Registry 配置状态检查及 C# 进程的环境转发/脱敏清单。
4. 在 `resources/voice_dubbing_voices.json` 添加该供应商自己的 VoiceProfile；提供外部 JSON 参数配置。页面、字幕校正、混音和 renderer 不增加供应商分支。
5. 新增 `tests/test_<provider>.py`，用模拟 HTTP 验证请求、错误、参数和真实文件时长；真实网络测试单独显式启用。验证跨 Provider 缓存隔离和 UI 切换。

本阶段没有新增第二家真实 API，不改变 ASR、字幕校正、文案时间、混音或视频合成算法，不接入调度器。

## 本次验证

- Python 离线回归 **123/123**；最终 CLI 与 Registry 调整另经 21 项 CLI、10 项 Registry 专项检查通过。
- WPF/真实 CLI 专项通过：四项服务选择、未配置门禁、注入服务的独立声音/模型、切回 ElevenLabs、自动保存、失败隔离、取消及历史项目恢复。
- 架构检查通过；Release 构建 0 警告、0 错误。明暗主题和最小窗口截图已检查。
- 使用已验收的 44.77 秒视频和已保存字幕完整生成 Bill 视频，禁止网络请求和 ASR：**21 句缓存命中，0 新语音，0 HTTP 请求，0 ASR**。raw/corrected 字节未变。产物位于 `tools/voice_dubbing/output/tts-registry-cache-check-d03a3e64/`，由 renderer 完整解码检查。
- 原视频、既有输出与持久缓存不覆盖。未运行第二家真实 API 或付费 integration test。
