using System;
using System.Collections.Generic;
using System.Text.Json.Serialization;
using System.Threading;
using System.Threading.Tasks;

namespace GaoxiaoVideo.Services;

public sealed record DubbingVoice(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("provider")] string Provider,
    [property: JsonPropertyName("voice_id")] string VoiceId,
    [property: JsonPropertyName("model_id")] string ModelId);

public sealed record DubbingSession(string SourceVideo, long DurationMs, string Directory, string? LastGeneration = null,
    bool HasSubtitles = false, IReadOnlyList<DubbingCombination>? LastSelection = null,
    string? AsrProvider = null, string? LastSingleGeneration = null);
public sealed record DubbingTextRow(string Id, int Index, long StartMs, long EndMs, string Text,
    bool Edited, bool Unsaved, string TextSource = "base");
public sealed record DubbingCorrection(bool Reviewed, bool HasDraft, IReadOnlyList<DubbingTextRow> Segments);
public sealed record DubbingScript(string Id, string Name, string Path, IReadOnlyList<DubbingTextRow> Segments);
public sealed record DubbingCombination(string ScriptId, string ScriptName, string VoiceId, string VoiceName,
    string Status, string? VideoPath = null, string? AudioPath = null, string? Error = null,
    string[]? Warnings = null, int CacheHits = 0, int Generated = 0, int Requests = 0);
public sealed record DubbingGeneration(string Directory, string Status, IReadOnlyList<DubbingCombination> Combinations);
public sealed record DubbingEnvironment(bool AsrReady, bool TtsReady, string Summary);
public sealed record DubbingSingleGeneration(string Directory, string Status, string Stage, DubbingVoice Voice,
    string? VideoPath = null, string? AudioPath = null, string? Error = null, string[]? Warnings = null,
    int CacheHits = 0, int Generated = 0, int Requests = 0, string? SubtitleFingerprint = null, bool IsCurrent = true,
    double ElapsedSeconds = 0);

/// <summary>UI boundary. All subtitle and script persistence is delegated to the Python core.</summary>
public interface IVoiceDubbingService : IAsyncDisposable
{
    VoiceDubbingConfiguration Configuration { get; }
    IReadOnlyList<DubbingVoice> Voices { get; }
    void Configure(VoiceDubbingConfiguration configuration);
    Task<DubbingEnvironment> CheckEnvironmentAsync(CancellationToken token = default);
    Task<DubbingSession?> RestoreSessionAsync(CancellationToken token = default);
    Task<DubbingSession> AddVideoAsync(string path, CancellationToken token = default);
    Task<DubbingCorrection> RecognizeAsync(DubbingSession session, CancellationToken token = default);
    Task<DubbingCorrection> CorrectionAsync(DubbingSession session, string operation = "inspect",
        string? id = null, string? text = null, CancellationToken token = default);
    Task<IReadOnlyList<DubbingScript>> ScriptsAsync(DubbingSession session, CancellationToken token = default);
    Task<DubbingScript> CreateScriptAsync(DubbingSession session, string name, DubbingScript? copy = null,
        CancellationToken token = default);
    Task<DubbingScript?> EditScriptAsync(DubbingSession session, DubbingScript script, string operation,
        string? id = null, string? text = null, CancellationToken token = default);
    Task<DubbingGeneration> GenerateAsync(DubbingSession session, IReadOnlyList<DubbingScript> scripts,
        IReadOnlyList<DubbingVoice> voices, IProgress<DubbingGeneration>? progress, CancellationToken token = default);
    Task<DubbingGeneration?> ReadGenerationAsync(string directory, CancellationToken token = default);
    Task<DubbingSingleGeneration> GenerateSingleAsync(DubbingSession session, DubbingVoice voice,
        IProgress<DubbingSingleGeneration>? progress, CancellationToken token = default);
    Task<DubbingSingleGeneration?> ReadSingleGenerationAsync(DubbingSession session, CancellationToken token = default);
}
