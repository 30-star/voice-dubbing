using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace GaoxiaoVideo.Services;

/// <summary>Transport and presentation projection over the frozen voice_dubbing CLI.</summary>
public sealed partial class VoiceDubbingService : IVoiceDubbingService
{
    private readonly IVoiceDubbingProcess _process;
    private readonly SemaphoreSlim _operations = new(1, 1);
    private readonly CancellationTokenSource _lifetime = new();
    private readonly bool _persist;
    private string? _configurationError;
    private string? _runningGeneration;
    public VoiceDubbingConfiguration Configuration { get; private set; }
    public IReadOnlyList<DubbingVoice> Voices { get; }
    private string Cache => string.IsNullOrWhiteSpace(Configuration.CacheDirectory)
        ? Path.Combine(Configuration.CoreDirectory, "cache", "tts") : Configuration.CacheDirectory;
    private string Output => string.IsNullOrWhiteSpace(Configuration.OutputDirectory)
        ? Path.Combine(Configuration.CoreDirectory, "output") : Configuration.OutputDirectory;
    private static string Subtitles(DubbingSession s) => Path.Combine(s.Directory, "subtitles");
    private static string Scripts(DubbingSession s) => Path.Combine(s.Directory, "scripts");

    public VoiceDubbingService(VoiceDubbingConfiguration? configuration = null, IVoiceDubbingProcess? process = null,
        string? voicesPath = null, bool persist = true)
    {
        try { Configuration = configuration ?? (File.Exists(VoiceDubbingConfiguration.SettingsPath)
                ? JsonSerializer.Deserialize<VoiceDubbingConfiguration>(File.ReadAllText(VoiceDubbingConfiguration.SettingsPath))
                    ?? throw new IOException("智能配音环境配置为空。") : new()); }
        catch (Exception ex) when (ex is IOException or JsonException) {
            Configuration = new(); _configurationError = "原环境配置无法读取，请重新保存设置。";
        }
        _process = process ?? new VoiceDubbingProcess();
        _persist = persist;
        voicesPath ??= Path.Combine(AppContext.BaseDirectory, "voice_dubbing", "voices.json");
        try { Voices = JsonSerializer.Deserialize<DubbingVoice[]>(File.ReadAllText(voicesPath)) ?? Array.Empty<DubbingVoice>(); }
        catch (Exception ex) when (ex is IOException or JsonException) { Voices = Array.Empty<DubbingVoice>(); }
        if (Voices.Count == 0 || Voices.Any(v => v == null || string.IsNullOrWhiteSpace(v.Id) || string.IsNullOrWhiteSpace(v.Name)
                || string.IsNullOrWhiteSpace(v.VoiceId) || string.IsNullOrWhiteSpace(v.ModelId) || v.Provider != "elevenlabs")
            || Voices.Select(v => v.Id).Distinct(StringComparer.OrdinalIgnoreCase).Count() != Voices.Count)
            Voices = Array.Empty<DubbingVoice>();
    }

    public void Configure(VoiceDubbingConfiguration configuration)
    {
        if (configuration.AsrProvider is not ("videocaptioner" or "faster-whisper") || configuration.AsrTimeoutSeconds <= 0
            || configuration.AsrTimeoutSeconds > 86400) throw new ArgumentException("请选择有效字幕识别方式及 1–86400 秒超时时间。");
        if (_persist && !SafeFileStore.Save(VoiceDubbingConfiguration.SettingsPath, configuration))
            throw new IOException("无法保存智能配音环境设置。");
        Configuration = configuration;
        _configurationError = null;
    }

    private async Task<JsonElement> CliAsync(string logDirectory, IEnumerable<string> arguments, CancellationToken token)
    {
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(token, _lifetime.Token);
        await _operations.WaitAsync(linked.Token).ConfigureAwait(false);
        try { return await ExecuteCliAsync(logDirectory, arguments, linked.Token).ConfigureAwait(false); }
        finally { _operations.Release(); }
    }

    private async Task<JsonElement> ExecuteCliAsync(string logDirectory, IEnumerable<string> arguments, CancellationToken token)
    {
        var args = new[] { "-B", "-X", "utf8", "-m", "voice_dubbing" }.Concat(arguments).ToArray();
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(120));
        using var bounded = CancellationTokenSource.CreateLinkedTokenSource(token, timeout.Token);
        var result = await _process.RunAsync(Configuration.PythonPath, args, Configuration.CoreDirectory,
            Path.Combine(logDirectory, Guid.NewGuid().ToString("N")), bounded.Token).ConfigureAwait(false);
        if (result.ExitCode != 0) throw new IOException("智能配音操作失败：" + VoiceDubbingProcess.Sanitize(result.Error.Trim()));
        using var document = JsonDocument.Parse(result.Output);
        return document.RootElement.Clone();
    }

    public async Task<DubbingEnvironment> CheckEnvironmentAsync(CancellationToken token = default)
    {
        var missing = new List<string>();
        if (!File.Exists(Configuration.PythonPath)) missing.Add("Python 路径");
        if (!File.Exists(Path.Combine(Configuration.CoreDirectory, "voice_dubbing", "__main__.py"))) missing.Add("配音核心目录");
        if (!File.Exists(Configuration.FFmpegPath)) missing.Add("FFmpeg 路径");
        if (!File.Exists(Configuration.FFprobePath)) missing.Add("FFprobe 路径");
        bool mediaReady = missing.Count == 0;
        if (missing.Count == 0)
        {
            using var linked = CancellationTokenSource.CreateLinkedTokenSource(token, _lifetime.Token);
            var result = await _process.RunAsync(Configuration.PythonPath,
                new[] { "-B", "-X", "utf8", "-c", Configuration.AsrProvider == "faster-whisper"
                    ? "import sys, faster_whisper; assert sys.version_info >= (3, 11)"
                    : "import sys; assert sys.version_info >= (3, 11)" },
                Configuration.CoreDirectory, Path.Combine(Output, "environment-check", Guid.NewGuid().ToString("N")), linked.Token).ConfigureAwait(false);
            if (result.ExitCode != 0) missing.Add("Python / 所选 ASR 依赖");
        }
        if (Configuration.AsrProvider is not ("videocaptioner" or "faster-whisper")) missing.Add("ASR Provider 配置");
        if (Configuration.AsrTimeoutSeconds <= 0) missing.Add("识别超时时间");
        if (Configuration.AsrProvider == "videocaptioner" && mediaReady)
        {
            if (!File.Exists(Configuration.VideoCaptionerCli)) missing.Add("卡卡 CLI 路径（桌面版 1.3.3 不支持 CLI）");
            else
            {
                using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(30));
                using var bounded = CancellationTokenSource.CreateLinkedTokenSource(token, timeout.Token, _lifetime.Token);
                try {
                    var check = await _process.RunAsync(Configuration.VideoCaptionerCli, new[] { "transcribe", "--help" },
                        Configuration.CoreDirectory, Path.Combine(Output, "environment-check", Guid.NewGuid().ToString("N")), bounded.Token).ConfigureAwait(false);
                    if (check.ExitCode != 0 || !check.Output.Contains("--asr")) missing.Add("卡卡 CLI 命令或依赖");
                } catch (OperationCanceledException) when (!token.IsCancellationRequested && !_lifetime.IsCancellationRequested) { missing.Add("卡卡 CLI 检查超时"); }
                catch (Exception ex) when (ex is IOException or System.ComponentModel.Win32Exception or InvalidOperationException)
                { missing.Add("卡卡 CLI 启动失败：" + VoiceDubbingProcess.Sanitize(ex.Message)); }
            }
        }
        bool key = !string.IsNullOrWhiteSpace(VoiceDubbingProcess.ApiKey);
        string summary = missing.Count == 0 ? (Configuration.AsrProvider == "videocaptioner"
            ? "卡卡 B 接口可用；识别需要联网。" : "Faster-Whisper 本地识别可用。") : "请检查：" + string.Join("、", missing) + "。";
        return new(missing.Count == 0, mediaReady && key && Voices.Count > 0,
            summary + (key ? " ElevenLabs 已配置。" : " ELEVENLABS_API_KEY 未配置。")
            + (Voices.Count == 0 ? " 声音配置缺失或无效。" : "") + _configurationError);
    }

    public Task<DubbingSession?> RestoreSessionAsync(CancellationToken token = default)
    {
        token.ThrowIfCancellationRequested();
        string? folder = Configuration.LastSessionDirectory;
        if (folder == null || !File.Exists(Path.Combine(folder, "session.json"))) return Task.FromResult<DubbingSession?>(null);
        return Task.FromResult(JsonSerializer.Deserialize<DubbingSession>(File.ReadAllText(Path.Combine(folder, "session.json"))));
    }

    private void SaveSession(DubbingSession session)
    {
        if (!SafeFileStore.Save(Path.Combine(session.Directory, "session.json"), session)) throw new IOException("无法保存配音项目索引。");
        Configure(Configuration with { LastSessionDirectory = session.Directory });
    }

    public async Task<DubbingSession> AddVideoAsync(string path, CancellationToken token = default)
    {
        if (!File.Exists(path)) throw new IOException("视频文件不存在。");
        string folder;
        do { folder = Path.Combine(Output, "ui-" + Guid.NewGuid().ToString("N")[..12]); }
        while (System.IO.Directory.Exists(folder));
        var probe = await _process.RunAsync(Configuration.FFprobePath,
            new[] { "-v", "error", "-show_entries", "format=duration", "-of", "json", Path.GetFullPath(path) },
            Configuration.CoreDirectory, Path.Combine(folder, "logs", "probe"), token).ConfigureAwait(false);
        if (probe.ExitCode != 0) throw new IOException("无法读取视频：" + probe.Error);
        using var document = JsonDocument.Parse(probe.Output);
        double seconds = double.Parse(document.RootElement.GetProperty("format").GetProperty("duration").GetString()!,
            System.Globalization.CultureInfo.InvariantCulture);
        if (!double.IsFinite(seconds) || seconds <= 0) throw new IOException("视频时长无效。");
        var session = new DubbingSession(Path.GetFullPath(path), (long)Math.Ceiling(seconds * 1000), folder);
        SaveSession(session);
        return session;
    }

    public async Task<DubbingCorrection> RecognizeAsync(DubbingSession session, CancellationToken token = default)
    {
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(token, _lifetime.Token);
        await _operations.WaitAsync(linked.Token).ConfigureAwait(false);
        try
        {
            if (File.Exists(Path.Combine(Subtitles(session), "raw_timeline.json"))) throw new IOException("该项目已有识别字幕，无需重复识别。");
            string attempt = Path.Combine(session.Directory, "recognition-attempts", Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(Path.GetDirectoryName(attempt)!);
            var arguments = new List<string>
                { "-B", "-X", "utf8", "-m", "voice_dubbing", "transcribe-video", session.SourceVideo,
                    "--output-dir", attempt, "--language", "zh", "--model", Configuration.AsrModel,
                    "--asr-provider", Configuration.AsrProvider, "--asr-timeout", Configuration.AsrTimeoutSeconds.ToString(System.Globalization.CultureInfo.InvariantCulture),
                    "--device", "cpu", "--compute-type", "int8", "--local-model-only",
                    "--ffmpeg", Configuration.FFmpegPath, "--ffprobe", Configuration.FFprobePath };
            if (Configuration.AsrProvider == "videocaptioner") arguments.AddRange(new[] { "--videocaptioner-cli", Configuration.VideoCaptionerCli });
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(Configuration.AsrTimeoutSeconds + 30));
            using var bounded = CancellationTokenSource.CreateLinkedTokenSource(linked.Token, timeout.Token);
            var result = await _process.RunAsync(Configuration.PythonPath,
                arguments, Configuration.CoreDirectory, Path.Combine(session.Directory, "logs", "recognize-" + Path.GetFileName(attempt)), bounded.Token).ConfigureAwait(false);
            if (result.ExitCode != 0) throw new IOException("识别失败：" + VoiceDubbingProcess.Sanitize(result.Error.Trim())
                + "；失败产物：" + attempt + "。可切换字幕识别方式后重试。");
            if (Directory.Exists(Subtitles(session)))
                Directory.Move(Subtitles(session), Path.Combine(session.Directory, "recognition-attempts", "previous-" + Guid.NewGuid().ToString("N")));
            Directory.Move(attempt, Subtitles(session));
            SaveSession(session with { HasSubtitles = true, AsrProvider = Configuration.AsrProvider });
        }
        finally { _operations.Release(); }
        return await CorrectionAsync(session, token: token).ConfigureAwait(false);
    }

    private static string Text(JsonElement obj, string name) => obj.TryGetProperty(name, out var value)
        && value.ValueKind == JsonValueKind.String ? value.GetString()! : "";
    private static bool Flag(JsonElement obj, string name) => obj.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.True;
    private static int Number(JsonElement obj, string name) => obj.TryGetProperty(name, out var value) && value.TryGetInt32(out int n) ? n : 0;

    public async Task<DubbingCorrection> CorrectionAsync(DubbingSession session, string operation = "inspect",
        string? id = null, string? text = null, CancellationToken token = default)
    {
        if (!new[] { "inspect", "set-text", "save", "discard", "restore", "restore-all" }.Contains(operation)) throw new ArgumentException("未知字幕操作。");
        var args = new List<string> { "correction", operation, Subtitles(session) };
        if (id != null) args.AddRange(new[] { "--segment-id", id });
        if (text != null) args.Add("--text=" + text);
        JsonElement data = await CliAsync(Path.Combine(session.Directory, "logs"), args, token).ConfigureAwait(false);
        var rows = data.GetProperty("segments").EnumerateArray().Select((s, index) => new DubbingTextRow(
            Text(s, "id"), index + 1, s.GetProperty("start_ms").GetInt64(), s.GetProperty("end_ms").GetInt64(),
            Text(s, "draft_text"), Flag(s, "draft_edited"), Flag(s, "unsaved"))).ToArray();
        return new(Flag(data, "reviewed"), Flag(data, "has_draft"), rows);
    }

    private async Task<DubbingScript> InspectScriptAsync(DubbingSession session, string path, CancellationToken token)
    {
        JsonElement data = await CliAsync(Path.Combine(session.Directory, "logs"), new[] { "script", "inspect", path,
            "--timeline", Subtitles(session) }, token).ConfigureAwait(false);
        return new(Text(data, "id"), Text(data, "name"), path, data.GetProperty("segments").EnumerateArray().Select((s, i) =>
            new DubbingTextRow(Text(s, "id"), i + 1, s.GetProperty("start_ms").GetInt64(), s.GetProperty("end_ms").GetInt64(),
                Text(s, "text"), Flag(s, "changed"), false, Text(s, "text_source"))).ToArray());
    }

    public async Task<IReadOnlyList<DubbingScript>> ScriptsAsync(DubbingSession session, CancellationToken token = default)
    {
        var result = new List<DubbingScript>();
        if (System.IO.Directory.Exists(Scripts(session))) foreach (string path in System.IO.Directory.GetFiles(Scripts(session), "*.json")
            .OrderBy(File.GetCreationTimeUtc).ThenBy(p => p, StringComparer.Ordinal))
            result.Add(await InspectScriptAsync(session, path, token).ConfigureAwait(false));
        return result;
    }

    public async Task<DubbingScript> CreateScriptAsync(DubbingSession session, string name, DubbingScript? copy = null,
        CancellationToken token = default)
    {
        string id = System.IO.Directory.Exists(Scripts(session)) ? "variant_" + Guid.NewGuid().ToString("N")[..12] : "original";
        string path = Path.Combine(Scripts(session), id + ".json");
        await CliAsync(Path.Combine(session.Directory, "logs"), new[] { "script", copy == null ? "create" : "copy",
            copy?.Path ?? Subtitles(session), "--id", id, "--name=" + name, "--output", path }, token).ConfigureAwait(false);
        return await InspectScriptAsync(session, path, token).ConfigureAwait(false);
    }

    public async Task<DubbingScript?> EditScriptAsync(DubbingSession session, DubbingScript script, string operation,
        string? id = null, string? text = null, CancellationToken token = default)
    {
        if (!new[] { "rename", "delete", "set-text", "restore" }.Contains(operation)) throw new ArgumentException("未知文案操作。");
        var args = new List<string> { "script", operation, script.Path };
        if (id != null) args.AddRange(new[] { "--segment-id", id });
        if (text != null) args.Add((operation == "rename" ? "--name=" : "--text=") + text);
        await CliAsync(Path.Combine(session.Directory, "logs"), args, token).ConfigureAwait(false);
        return operation == "delete" ? null : await InspectScriptAsync(session, script.Path, token).ConfigureAwait(false);
    }

    public async Task<DubbingGeneration?> ReadGenerationAsync(string directory, CancellationToken token = default)
    {
        try
        {
            string path = Path.Combine(directory, "script_batch_report.json");
            if (!File.Exists(path)) return null;
            await using var stream = new FileStream(path, FileMode.Open, FileAccess.Read,
                FileShare.ReadWrite | FileShare.Delete, 4096, FileOptions.Asynchronous);
            using var document = await JsonDocument.ParseAsync(stream, cancellationToken: token).ConfigureAwait(false);
            JsonElement data = document.RootElement;
            var completed = data.GetProperty("combinations").EnumerateArray().Select(s => {
                JsonElement voice = s.GetProperty("voice");
                return new DubbingCombination(Text(s, "script_id"), Text(s, "script_name"), Text(voice, "id"), Text(voice, "name"),
                    Text(s, "status"), Text(s, "video_path"), Text(s, "audio_path"), Text(s, "error"),
                    s.GetProperty("warnings").EnumerateArray().Select(x => x.GetString()!).ToArray(),
                    Number(s, "cache_hits"), Number(s, "generated_segments"), Number(s, "supplier_requests"));
            }).ToArray();
            string status = Text(data, "status");
            if (status == "running" && _runningGeneration != directory) {
                // Restore the UI-owned selection index; never restart an interrupted paid request.
                string sessionPath = Path.Combine(Path.GetDirectoryName(Path.GetDirectoryName(directory))!, "session.json");
                if (File.Exists(sessionPath)) {
                    var session = JsonSerializer.Deserialize<DubbingSession>(File.ReadAllText(sessionPath));
                    if (session?.LastGeneration == directory && session.LastSelection != null)
                        completed = session.LastSelection.Select(row => completed.FirstOrDefault(r => r.ScriptId == row.ScriptId
                            && r.VoiceId == row.VoiceId) ?? row with { Status = "not_run", Error = "上次操作已中断，未自动重试。" }).ToArray();
                }
                status = "interrupted";
            }
            return new(directory, status, completed);
        }
        catch (IOException) { return null; } // File replacement can briefly conflict with a reader on Windows.
        catch (JsonException) { return null; }
        catch (InvalidOperationException) { return null; }
        catch (KeyNotFoundException) { return null; }
    }

    public static DubbingGeneration ProjectProgress(string folder, IReadOnlyList<DubbingScript> scripts,
        IReadOnlyList<DubbingVoice> voices, DubbingGeneration? report, bool running, string? error = null)
    {
        bool active = false;
        var rows = new List<DubbingCombination>();
        foreach (var script in scripts) foreach (var voice in voices)
        {
            var completed = report?.Combinations.FirstOrDefault(r => r.ScriptId == script.Id && r.VoiceId == voice.Id);
            if (completed != null) { rows.Add(completed); continue; }
            string status = running ? active || report == null ? "waiting" : "running" : error == null ? "not_run" : active ? "not_run" : "failed";
            if (running && report != null && !active || !running && error != null && !active) active = true;
            rows.Add(new(script.Id, script.Name, voice.Id, voice.Name, status, Error: status == "failed" ? error : null));
        }
        return new(folder, report?.Status ?? (running ? "running" : "failed"), rows);
    }

    public async Task<DubbingGeneration> GenerateAsync(DubbingSession session, IReadOnlyList<DubbingScript> scripts,
        IReadOnlyList<DubbingVoice> voices, IProgress<DubbingGeneration>? progress, CancellationToken token = default)
    {
        // Freeze caller collections before yielding; callers cannot mutate a running selection.
        scripts = scripts.ToArray(); voices = voices.ToArray();
        if (scripts.Count == 0 || voices.Count == 0) throw new IOException("请选择文案版本和声音。");
        var correction = await CorrectionAsync(session, token: token).ConfigureAwait(false);
        if (!correction.Reviewed || correction.HasDraft) throw new IOException("请先保存并确认字幕；未保存修改不能配音。");
        string run;
        do { run = Guid.NewGuid().ToString("N")[..12]; }
        while (System.IO.Directory.Exists(Path.Combine(session.Directory, "generations", run)));
        string folder = Path.Combine(session.Directory, "generations", run);
        string manifest = Path.Combine(session.Directory, "generation-manifests", run + ".json");
        var jobs = scripts.Select(s => new { script = s.Path, voices }).ToArray();
        if (!SafeFileStore.Save(manifest, jobs)) throw new IOException("无法保存本次配音清单。");
        SaveSession(session with { LastGeneration = folder, LastSelection = scripts.SelectMany(script => voices.Select(voice =>
            new DubbingCombination(script.Id, script.Name, voice.Id, voice.Name, "waiting"))).ToArray() });
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(token, _lifetime.Token);
        await _operations.WaitAsync(linked.Token).ConfigureAwait(false);
        DubbingGeneration? latestReport = null;
        try
        {
            _runningGeneration = folder;
            var command = _process.RunAsync(Configuration.PythonPath, new[] { "-B", "-X", "utf8", "-m", "voice_dubbing",
                "dub-scripts", session.SourceVideo, "--timeline", Subtitles(session), "--variants-file", manifest,
                "--output-dir", folder, "--cache-dir", Cache, "--language", "zh", "--output-format", "mp3_44100_128",
                "--ffmpeg", Configuration.FFmpegPath, "--ffprobe", Configuration.FFprobePath },
                Configuration.CoreDirectory, Path.Combine(session.Directory, "logs", "generate-" + run), linked.Token);
            progress?.Report(ProjectProgress(folder, scripts, voices, null, true));
            while (!command.IsCompleted)
            {
                await Task.WhenAny(command, Task.Delay(1000, linked.Token)).ConfigureAwait(false);
                if (linked.IsCancellationRequested) await command.ConfigureAwait(false); // Await owned process termination before releasing the service.
                latestReport = await ReadGenerationAsync(folder, linked.Token).ConfigureAwait(false) ?? latestReport;
                progress?.Report(ProjectProgress(folder, scripts, voices, latestReport, true));
            }
            var result = await command.ConfigureAwait(false);
            var report = await ReadGenerationAsync(folder, linked.Token).ConfigureAwait(false) ?? latestReport;
            var final = ProjectProgress(folder, scripts, voices, report, false,
                result.ExitCode is 0 or 3 && report != null ? null : VoiceDubbingProcess.Sanitize(
                    string.IsNullOrWhiteSpace(result.Error) ? "核心进程未返回有效的批量报告。" : result.Error.Trim()));
            progress?.Report(final);
            return final;
        }
        catch (OperationCanceledException)
        {
            var report = await ReadGenerationAsync(folder).ConfigureAwait(false) ?? latestReport;
            var stopped = ProjectProgress(folder, scripts, voices, report, false);
            stopped = stopped with { Status = "cancelled", Combinations = stopped.Combinations.Select(r =>
                r.Status == "not_run" ? r with { Status = "cancelled" } : r).ToArray() };
            progress?.Report(stopped);
            throw;
        }
        catch (Exception ex)
        {
            var report = await ReadGenerationAsync(folder).ConfigureAwait(false) ?? latestReport;
            var failed = ProjectProgress(folder, scripts, voices, report, false, VoiceDubbingProcess.Sanitize(ex.Message));
            failed = failed with { Status = "failed" };
            progress?.Report(failed);
            return failed;
        }
        finally { _runningGeneration = null; _operations.Release(); }
    }

    public async ValueTask DisposeAsync()
    {
        _lifetime.Cancel();
        await _operations.WaitAsync().ConfigureAwait(false);
        _operations.Release();
        _lifetime.Dispose();
    }
}
