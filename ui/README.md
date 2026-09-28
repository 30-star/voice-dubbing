# Windows UI / 嵌入 WPF

这里只包含配音专用页面、ViewModel、C# CLI 连接服务、通用 JSON 原子保存辅助类，以及最小宿主，不包含影匠主程序、任务调度器或其他业务。

要求 Windows 和 .NET 8 SDK。Python、FFmpeg、FFprobe、模型和外部 VideoCaptioner CLI 仍须单独配置。

从仓库根目录启动：

```powershell
dotnet run --project ui/desktop/VoiceDubbing.Desktop.csproj -c Release
```

界面沿用单视频、单声音、单输出流程。展开环境设置配置实际可执行文件位置。声音来自 `config/voices.json`，密钥仍从环境变量读取；独立 UI 使用 `%APPDATA%/VoiceDubbing/voice-dubbing-service.json`，不读取或修改影匠用户设置。运行目录中必须存在 `voice_dubbing/voices.json`（桌面项目自动复制）。

其他 WPF 程序可引用 `ui/wpf/VoiceDubbing.Wpf.csproj`，合并 `Styles.xaml`，嵌入 `GaoxiaoVideo.Views.VoiceDubbingPage`。原命名空间保留以避免改写已验证的页面代码。可通过构造函数注入 `IVoiceDubbingService`；显式配置 `CoreDirectory`、Python 和媒体工具路径。应用关闭时等待 `page.ShutdownAsync()`，终止本功能拥有的进程树。

发布示例：

```powershell
dotnet publish ui/desktop/VoiceDubbing.Desktop.csproj -c Release -o artifacts/desktop
```

发布目录只带配音 UI 和必要 Python 源码，不带 Python 运行时、FFmpeg、模型、缓存或密钥。打包发现与设置目录是独立副本的唯一连接配置调整，Python 业务源码保持一致。
