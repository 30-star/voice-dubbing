# Noiz TTS 接入

Noiz 通过独立 `NoizTTSProvider` 接入统一 Provider Registry。ElevenLabs、Bill/Sarah 配置及现有缓存继续保留。

## 官方接口依据

核对日期：2026-09-29。旧 `/docs-text-to-speech` 路径返回 404，当前以 [官方 OpenAPI](https://developers.noiz.ai/openapi.yaml) 为依据。

- `POST https://noiz.ai/v1/text-to-speech`，`multipart/form-data`。
- `Authorization` 请求头直接使用 API Key，不能额外添加 Bearer。
- 文本、声音分别为 `text`、`voice_id`；中文语言传 `target_lang=zh`。
- 非流式 `stream=false`，每次最多 5000 字符。返回完整音频二进制，支持 `wav`、`mp3`。
- 参数包括 `quality_preset`、`speed`、`duration`、`similarity_enh`、`trim_silence`、`emo`。首版不提供声音克隆、保存声音、流式输出。
- 官方接口没有公开可选的 `model_id`。内部 VoiceProfile 使用 `noiz-v1` 作为路由与缓存身份，不向 API 发送该字段；不接受 ElevenLabs 模型名。
- 声音通过 `GET /v1/voices?voice_type=built-in|custom&skip=<页号>&limit=100` 分页读取。页面由服务层调用 `tts-voices` CLI，HTTP 仅在 Adapter 中。

## 用户配置和使用

在 Windows 用户或系统环境变量中设置 `NOIZ_API_KEY`。不保存到源码、JSON、命令参数或聊天中。

智能配音页面中选择 **Noiz**。没有密钥时显示“未配置”，不能生成；配置后使用运行环境设置的检查入口，再选择 Noiz，自动读取账户及系统声音。也可点击“刷新声音”。读取失败会清空该服务的可选声音并显示原因，不沿用 Bill/Sarah，不自动切回其他服务。

可选配置使用模块的 `examples/tts-providers.json`，页面运行环境设置的 TTS 配置路径指向该文件。`speed` 为真实 Noiz 请求语速，默认 1；现有后处理时长匹配独立计算，不修改它或 TTS 缓存键。

CLI：

```powershell
python -m voice_dubbing tts-providers
python -m voice_dubbing tts-voices --provider noiz
python -m voice_dubbing dub-timeline '<已保存校正字幕目录>' `
  --provider noiz --voice '<从声音列表选择的 voice_id>' `
  --language zh --cache-dir cache/tts
```

字幕目录必须已经保存确认。单输出页面继续调用 `dub-video --timeline ... --voices-file ...`，声音清单只包含所选的 Noiz 声音，不重新执行 ASR。模型、格式、参数不与 ElevenLabs 共享。

## 缓存、产物和失败处理

缓存包含 Provider、内部模型身份、声音、文本、语言、格式、语速及所有有效音频参数。Noiz 默认参数也进入缓存，不能复用 ElevenLabs 条目。密钥和请求超时不进入缓存。

每句保留 `speech.original.wav` 或 `speech.original.mp3`、24kHz 单声道 `speech.wav` 和请求/解码日志。缓存命中仍复制原音频到本次任务。实际时长根据 WAV 样本测量。继续使用 corrected 字幕、150ms 容差和超长句匹配；原始 TTS 保留，匹配结果不污染缓存。

429/5xx 重试遵守 `Retry-After`，单次等待最多 60 秒；认证及参数错误不重试。超时或读取中断不重试可能已经扣费的请求。不回退为 Fake 或 ElevenLabs。失败音频不入缓存。

## 验证

普通测试只使用模拟 HTTP，不请求 Noiz：

```powershell
python -m unittest discover -s tests -v
```

单独真实测试必须显式启用并选择声音，将发送一条可能计费的中文请求：

```powershell
$env:VOICE_DUBBING_RUN_NOIZ_INTEGRATION = '1'
$env:NOIZ_VOICE_ID = '<已选择的 voice_id>'
python -m unittest discover -s integration_tests -p test_noiz_live.py -v
```

缺少密钥或明确选择的声音时跳过，不用 Fake 替代真实验收。

Noiz 限流可能通过 HTTP 200 JSON 返回，例如 `You've hit the rate limit. Please retry in 20 seconds`。Adapter 将这种明确拒绝识别为限流，优先读取 `retry_after` 或提示中的等待秒数，并在同一账户的所有并发 Adapter 间共享冷却时间。默认最多重试 2 次；错误 JSON 不保存为音频、不进入缓存。余额不足、认证失败和结果不确定的超时不因本机制重试。成功片段保持缓存，下次生成只处理尚未缓存的文字，不重新识别视频。
