using System;
using System.IO;

namespace GaoxiaoVideo.Services;

public sealed record VoiceDubbingConfiguration
{
    public string PythonPath { get; init; } = FindExecutable("python.exe");
    public string CoreDirectory { get; init; } = FindCore();
    public string FFmpegPath { get; init; } = FindExecutable("ffmpeg.exe");
    public string FFprobePath { get; init; } = FindExecutable("ffprobe.exe");
    public string CacheDirectory { get; init; } = "";
    public string OutputDirectory { get; init; } = "";
    public string AsrModel { get; init; } = "base";
    public string AsrProvider { get; init; } = "videocaptioner";
    public string VideoCaptionerCli { get; init; } = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "VideoCaptionerCLI", "venv", "Scripts", "videocaptioner.exe");
    public int AsrTimeoutSeconds { get; init; } = 600;
    public string? LastSessionDirectory { get; init; }
    public string? LastVoiceId { get; init; }
    public string TtsProvider { get; init; } = "elevenlabs";
    public string TtsConfigurationPath { get; init; } = "";
    public int TtsConcurrency { get; init; } = 4;
    public static string SettingsPath => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "VoiceDubbing", "voice-dubbing-service.json");

    public static string FindExecutable(string name)
    {
        foreach (string directory in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator))
        {
            try { string candidate = Path.Combine(directory.Trim('"'), name); if (File.Exists(candidate)) return Path.GetFullPath(candidate); }
            catch (ArgumentException) { }
        }
        string bundled = Path.Combine(AppContext.BaseDirectory, "ffmpeg", name);
        return File.Exists(bundled) ? bundled : "";
    }

    private static string FindCore()
    {
        // Discover the standalone repository; published builds carry only Python sources under core/.
        for (DirectoryInfo? root = new(AppContext.BaseDirectory); root != null; root = root.Parent)
        {
            string candidate = root.FullName;
            if (File.Exists(Path.Combine(candidate, "pyproject.toml")) && File.Exists(Path.Combine(candidate, "voice_dubbing", "cli.py"))) return candidate;
        }
        return Path.Combine(AppContext.BaseDirectory, "core");
    }
}
