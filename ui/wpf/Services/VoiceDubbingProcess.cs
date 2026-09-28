using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace GaoxiaoVideo.Services;

public sealed record DubbingProcessResult(int ExitCode, string Output, string Error);
public interface IVoiceDubbingProcess
{
    Task<DubbingProcessResult> RunAsync(string executable, IReadOnlyList<string> arguments, string directory,
        string logPrefix, CancellationToken token);
}

/// <summary>Owns only the dubbing child; never registers it with the main task scheduler.</summary>
public sealed class VoiceDubbingProcess : IVoiceDubbingProcess
{
    internal static string? ApiKey => new[] { EnvironmentVariableTarget.Process, EnvironmentVariableTarget.User, EnvironmentVariableTarget.Machine }
        .Select(target => Environment.GetEnvironmentVariable("ELEVENLABS_API_KEY", target))
        .FirstOrDefault(value => !string.IsNullOrWhiteSpace(value));
    internal static string Sanitize(string text) => string.IsNullOrEmpty(ApiKey) ? text : text.Replace(ApiKey, "[REDACTED]", StringComparison.Ordinal);

    public async Task<DubbingProcessResult> RunAsync(string executable, IReadOnlyList<string> arguments,
        string directory, string logPrefix, CancellationToken token)
    {
        var info = new ProcessStartInfo(executable) { WorkingDirectory = directory, UseShellExecute = false,
            CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8 };
        foreach (string arg in arguments) info.ArgumentList.Add(arg);
        info.Environment["PYTHONUTF8"] = "1";
        bool captioner = Path.GetFileNameWithoutExtension(executable).Equals("videocaptioner", StringComparison.OrdinalIgnoreCase);
        if (captioner)
        {
            string profile = Path.Combine(Path.GetDirectoryName(logPrefix)!, Path.GetFileName(logPrefix) + "-profile");
            Directory.CreateDirectory(profile);
            info.WorkingDirectory = profile;
            info.Environment["USERPROFILE"] = profile;
            info.Environment["TEMP"] = profile;
            info.Environment["TMP"] = profile;
            info.Environment["WIN_PD_OVERRIDE_LOCAL_APPDATA"] = Path.Combine(profile, "AppData", "Local");
            info.Environment["WIN_PD_OVERRIDE_APPDATA"] = Path.Combine(profile, "AppData", "Roaming");
            info.Environment.Remove("ELEVENLABS_API_KEY");
            info.Environment.Remove("OPENAI_API_KEY");
        }
        if (!captioner && !string.IsNullOrEmpty(ApiKey)) info.Environment["ELEVENLABS_API_KEY"] = ApiKey;
        using var child = new Process { StartInfo = info };
        var gate = new object();
        bool started = false;
        using var registration = token.Register(() => {
            lock (gate) { if (started) try { if (!child.HasExited) child.Kill(entireProcessTree: true); } catch (InvalidOperationException) { } }
        });
        lock (gate) { token.ThrowIfCancellationRequested(); started = child.Start(); }
        if (!started) throw new IOException("无法启动智能配音组件。");
        Task<string> stdout = child.StandardOutput.ReadToEndAsync();
        Task<string> stderr = child.StandardError.ReadToEndAsync();
        try { await child.WaitForExitAsync(token).ConfigureAwait(false); }
        finally
        {
            if (token.IsCancellationRequested) {
                try { if (!child.HasExited) child.Kill(entireProcessTree: true); } catch (InvalidOperationException) { }
                await child.WaitForExitAsync().ConfigureAwait(false);
            }
            string output = Sanitize(await stdout.ConfigureAwait(false));
            string error = Sanitize(await stderr.ConfigureAwait(false));
            System.IO.Directory.CreateDirectory(Path.GetDirectoryName(logPrefix)!);
            await File.WriteAllTextAsync(logPrefix + ".stdout.log", output).ConfigureAwait(false);
            await File.WriteAllTextAsync(logPrefix + ".stderr.log", error).ConfigureAwait(false);
        }
        return new(child.ExitCode, Sanitize(await stdout.ConfigureAwait(false)), Sanitize(await stderr.ConfigureAwait(false)));
    }
}
