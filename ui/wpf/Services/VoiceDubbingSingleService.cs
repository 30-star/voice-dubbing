using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace GaoxiaoVideo.Services;

public sealed partial class VoiceDubbingService
{
    private static string SubtitleFingerprint(DubbingSession session) => Convert.ToHexString(
        SHA256.HashData(File.ReadAllBytes(Path.Combine(Subtitles(session), "corrected_timeline.json"))));
    private static string SingleIndex(string directory) => Path.Combine(
        Path.GetDirectoryName(Path.GetDirectoryName(directory))!, "generation-manifests", Path.GetFileName(directory) + "-single.json");

    public async Task<DubbingSingleGeneration?> ReadSingleGenerationAsync(DubbingSession session, CancellationToken token = default)
    {
        if (session.LastSingleGeneration is not { } directory || !File.Exists(SingleIndex(directory))) return null;
        var index = JsonSerializer.Deserialize<DubbingSingleGeneration>(await File.ReadAllTextAsync(SingleIndex(directory), token));
        if (index == null) return null;
        var result = await SingleReportAsync(index, token).ConfigureAwait(false);
        if (result.Status == "running" && _runningGeneration != directory)
            result = result with { Status = "interrupted", Stage = "上次生成已中断", Error = "保留已有文件，未自动重试。" };
        return result with { IsCurrent = result.SubtitleFingerprint == SubtitleFingerprint(session)
            && !File.Exists(Path.Combine(Subtitles(session), "correction_draft.json")) };
    }

    private async Task<DubbingSingleGeneration> SingleReportAsync(DubbingSingleGeneration index, CancellationToken token)
    {
        if (index.Status != "running") return index;
        string path = Path.Combine(index.Directory, "batch_report.json");
        try
        {
            if (File.Exists(path))
            {
                await using var stream = new FileStream(path, FileMode.Open, FileAccess.Read,
                    FileShare.ReadWrite | FileShare.Delete, 4096, FileOptions.Asynchronous);
                using var document = await JsonDocument.ParseAsync(stream, cancellationToken: token).ConfigureAwait(false);
                var data = document.RootElement;
                if (data.GetProperty("asr_calls").GetInt32() != 0 || data.GetProperty("total_voices").GetInt32() != 1)
                    throw new IOException("单输出报告包含意外识别或多个声音。");
                var rows = data.GetProperty("voices").EnumerateArray().ToArray();
                if (rows.Length == 1)
                {
                    var row = rows[0];
                    var voice = row.GetProperty("voice");
                    if (Text(voice, "id") != index.Voice.Id || Text(voice, "provider") != index.Voice.Provider
                        || Text(voice, "voice_id") != index.Voice.VoiceId || Text(voice, "model_id") != index.Voice.ModelId)
                        throw new IOException("生成报告服务、模型或声音不匹配。");
                    string status = Text(row, "status"), video = Text(row, "video_path");
                    if (status == "succeeded" && !File.Exists(video)) throw new IOException("生成报告的视频文件不存在。");
                    return index with { Status = status, Stage = status == "succeeded" ? "完成" : "生成失败",
                        VideoPath = video, AudioPath = Text(row, "audio_path"), Error = Text(row, "error"),
                        Warnings = row.GetProperty("warnings").EnumerateArray().Select(x => x.GetString()!).ToArray(),
                        CacheHits = Number(row, "cache_hits"), Generated = Number(row, "generated_segments"), Requests = Number(row, "supplier_requests") };
                }
            }
        }
        catch (JsonException) { } // Atomic replacement or a partially readable report is retried on the next poll.
        string audio = Path.Combine(index.Directory, index.Voice.Id, "dubbed_audio.wav");
        return index with { Stage = File.Exists(audio) ? "合成并检查视频" : "生成配音音轨" };
    }

    public async Task<DubbingSingleGeneration> GenerateSingleAsync(DubbingSession session, DubbingVoice voice,
        IProgress<DubbingSingleGeneration>? progress, CancellationToken token = default)
    {
        if (!Voices.Contains(voice) || voice.Provider != Configuration.TtsProvider)
            throw new IOException("请选择当前配音服务配置中的一个有效声音。");
        var correction = await CorrectionAsync(session, token: token).ConfigureAwait(false);
        if (!correction.Reviewed || correction.HasDraft) throw new IOException("请先保存并确认字幕。");
        string run = Guid.NewGuid().ToString("N")[..12];
        string folder = Path.Combine(session.Directory, "generations", run);
        string manifest = Path.Combine(session.Directory, "generation-manifests", run + "-voice.json");
        var index = new DubbingSingleGeneration(folder, "running", "生成配音音轨", voice,
            SubtitleFingerprint: SubtitleFingerprint(session));
        if (!SafeFileStore.Save(manifest, new[] { voice }) || !SafeFileStore.Save(SingleIndex(folder), index))
            throw new IOException("无法保存本次单声音生成配置。");
        SaveSession(session with { LastSingleGeneration = folder });
        Configure(Configuration with { LastVoiceId = voice.Id });
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(token, _lifetime.Token);
        await _operations.WaitAsync(linked.Token).ConfigureAwait(false);
        var elapsed = Stopwatch.StartNew();
        Task<DubbingProcessResult>? command = null;
        try
        {
            _runningGeneration = folder;
            var arguments = new System.Collections.Generic.List<string> { "-B", "-X", "utf8", "-m", "voice_dubbing",
                "dub-video", session.SourceVideo, "--timeline", Subtitles(session), "--voices-file", manifest,
                "--output-dir", folder, "--cache-dir", Cache, "--language", "zh",
                "--ffmpeg", Configuration.FFmpegPath, "--ffprobe", Configuration.FFprobePath };
            if (!string.IsNullOrWhiteSpace(Configuration.TtsConfigurationPath))
                arguments.AddRange(new[] { "--tts-config", Configuration.TtsConfigurationPath });
            arguments.AddRange(new[] { "--tts-concurrency", Configuration.TtsConcurrency.ToString(System.Globalization.CultureInfo.InvariantCulture) });
            command = _process.RunAsync(Configuration.PythonPath, arguments,
                Configuration.CoreDirectory, Path.Combine(session.Directory, "logs", "single-" + run), linked.Token);
            progress?.Report(index);
            while (!command.IsCompleted)
            {
                await Task.WhenAny(command, Task.Delay(1000, linked.Token)).ConfigureAwait(false);
                if (linked.IsCancellationRequested) await command.ConfigureAwait(false);
                progress?.Report(await SingleReportAsync(index, linked.Token).ConfigureAwait(false));
            }
            var exit = await command.ConfigureAwait(false);
            var result = await SingleReportAsync(index, linked.Token).ConfigureAwait(false);
            if (exit.ExitCode != 0 || result.Status == "running")
                result = result with { Status = "failed", Stage = "生成失败", VideoPath = null,
                    Error = string.IsNullOrWhiteSpace(result.Error) ? VoiceDubbingProcess.Sanitize(
                        string.IsNullOrWhiteSpace(exit.Error) ? "核心进程未返回有效的单输出结果。" : exit.Error.Trim()) : result.Error };
            index = result with { ElapsedSeconds = elapsed.Elapsed.TotalSeconds };
            return index;
        }
        catch (OperationCanceledException)
        {
            index = index with { Status = "cancelled", Stage = "已停止", Error = "操作已停止，已有文件保留。" };
            throw;
        }
        catch (Exception ex)
        {
            index = index with { Status = "failed", Stage = "生成失败", Error = VoiceDubbingProcess.Sanitize(ex.Message) };
            return index;
        }
        finally
        {
            // A report/transport failure must not leave a paid child running after the UI unlocks.
            if (command is { IsCompleted: false })
            {
                linked.Cancel();
                try { await command.ConfigureAwait(false); } catch (Exception) { /* Preserve the original failure and process logs. */ }
            }
            index = index with { ElapsedSeconds = elapsed.Elapsed.TotalSeconds };
            SafeFileStore.Save(SingleIndex(folder), index);
            progress?.Report(index);
            _runningGeneration = null; _operations.Release();
        }
    }
}
