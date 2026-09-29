using GaoxiaoVideo.Services;
using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.IO;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Threading;
using System.Threading.Tasks;

namespace GaoxiaoVideo.Views;

public sealed class DubbingEditableRow : INotifyPropertyChanged
{
    public DubbingTextRow Model { get; }
    public string Id => Model.Id;
    public int Index => Model.Index;
    public string Start => TimeSpan.FromMilliseconds(Model.StartMs).ToString(@"hh\:mm\:ss\.fff");
    public string End => TimeSpan.FromMilliseconds(Model.EndMs).ToString(@"hh\:mm\:ss\.fff");
    private string _text;
    public string Text { get => _text; set { _text = value; PropertyChanged?.Invoke(this, new(nameof(Text))); } }
    public string Status => Model.Unsaved ? "已修改／未保存" : Model.Edited ? "已修改" : "原始";
    public string Source => Model.TextSource == "override" ? "自定义" : "继承基础";
    public DubbingEditableRow(DubbingTextRow model) { Model = model; _text = model.Text; }
    public event PropertyChangedEventHandler? PropertyChanged;
}

public sealed class DubbingVoiceChoice : INotifyPropertyChanged
{
    public DubbingVoice Model { get; }
    public string Name => Model.Name;
    private bool _selected = true;
    public bool Selected { get => _selected; set { _selected = value; PropertyChanged?.Invoke(this, new(nameof(Selected))); Changed?.Invoke(); } }
    public event Action? Changed;
    public event PropertyChangedEventHandler? PropertyChanged;
    public DubbingVoiceChoice(DubbingVoice model) => Model = model;
}

public sealed record DubbingResultRow(DubbingCombination Model)
{
    public string ScriptName => Model.ScriptName;
    public string VoiceName => Model.VoiceName;
    public string Status => Model.Status switch { "waiting" => "等待", "running" => "生成中", "succeeded" => "完成",
        "failed" => "失败", "cancelled" => "已停止", _ => "未执行" };
    public string Details => string.Join("；", new[] { Model.Error }.Concat(Model.Warnings ?? Array.Empty<string>()).Where(s => !string.IsNullOrWhiteSpace(s)));
    public string Cache => $"命中 {Model.CacheHits} / 新生成 {Model.Generated}";
}

public sealed record DubbingAsrChoice(string Id, string Name);

/// <summary>Presentation state only. Core text validation, persistence and execution stay in the service.</summary>
public sealed class VoiceDubbingViewModel : INotifyPropertyChanged, IAsyncDisposable
{
    private readonly IVoiceDubbingService _service;
    private CancellationTokenSource? _operationCancellation;
    private readonly CancellationTokenSource _lifetime = new();
    private Task _edits = Task.CompletedTask;
    private int _pending;
    private bool _busy, _reviewed, _draft, _editFailed, _asrReady, _ttsReady;
    private DubbingScript? _selectedScript;
    public DubbingSession? Session { get; private set; }
    public DubbingGeneration? Generation { get; private set; }
    public DubbingSingleGeneration? SingleGeneration { get; private set; }
    private DubbingVoiceChoice? _selectedVoice;
    public DubbingVoiceChoice? SelectedVoice { get => _selectedVoice; set {
        _selectedVoice = value != null && Voices.Contains(value) && value.Model.Provider == SelectedProvider?.Id ? value : null;
        if (_selectedVoice != null) try { _service.Configure(_service.Configuration with { LastVoiceId = _selectedVoice.Model.Id }); }
            catch (Exception ex) { Message = ex.Message; }
        Changed();
    } }
    public ObservableCollection<DubbingTtsProvider> Providers { get; } = new();
    private DubbingTtsProvider? _selectedProvider;
    private string _voiceStatus = "";
    public DubbingTtsProvider? SelectedProvider { get => _selectedProvider; set {
        if (_busy || value == null || !Providers.Contains(value) || value.Id == _selectedProvider?.Id) return;
        _selectedProvider = value;
        _voiceStatus = "";
        RebuildVoices(null);
        try { _service.Configure(_service.Configuration with { TtsProvider = value.Id, LastVoiceId = _selectedVoice?.Model.Id }); }
        catch (Exception ex) { Message = ex.Message; }
        Changed();
        if (value.Ready && value.SupportsVoiceListing) _ = RefreshVoicesAsync();
    } }
    public string ProviderStatus => (SelectedProvider?.Status ?? "未配置") + (string.IsNullOrEmpty(_voiceStatus) ? "" : " · " + _voiceStatus);
    public string TtsConfigurationPath { get; set; }
    public int TtsConcurrency { get; set; }
    private void RebuildVoices(string? preferred)
    {
        Voices.Clear();
        foreach (var voice in _service.Voices.Where(v => v.Provider == _selectedProvider?.Id))
        {
            var choice = new DubbingVoiceChoice(voice); choice.Changed += Changed; Voices.Add(choice);
        }
        _selectedVoice = Voices.FirstOrDefault(v => v.Model.Id == preferred) ?? Voices.FirstOrDefault();
    }
    private void RefreshProviders()
    {
        string id = _selectedProvider?.Id ?? _service.Configuration.TtsProvider;
        string? voice = _selectedVoice?.Model.Id ?? _service.Configuration.LastVoiceId;
        Providers.Clear(); foreach (var provider in _service.Providers) Providers.Add(provider);
        _selectedProvider = Providers.FirstOrDefault(p => p.Id == id) ?? Providers.FirstOrDefault();
        RebuildVoices(voice); Changed();
    }
    private async Task LoadSelectedVoicesAsync(CancellationToken token)
    {
        var provider = SelectedProvider;
        if (provider is not { Ready: true, SupportsVoiceListing: true }) { _voiceStatus = ""; return; }
        string? preferred = _selectedVoice?.Model.Id ?? _service.Configuration.LastVoiceId;
        Voices.Clear(); _selectedVoice = null; _voiceStatus = "正在读取声音列表…"; Changed();
        try {
            await _service.RefreshVoicesAsync(provider.Id, token);
            RebuildVoices(preferred);
            _voiceStatus = Voices.Count == 0 ? "没有可用声音" : $"已载入 {Voices.Count} 个声音";
            if (_selectedVoice != null) _service.Configure(_service.Configuration with { LastVoiceId = _selectedVoice.Model.Id });
        }
        catch (OperationCanceledException) { _voiceStatus = "声音读取已停止"; throw; }
        catch (Exception ex) { _voiceStatus = "声音读取失败：" + ex.Message; Message = ex.Message; }
        Changed();
    }
    public Task RefreshVoicesAsync() => Operate(LoadSelectedVoicesAsync);
    public string OutputPath => SingleGeneration?.VideoPath ?? "";
    public bool CanPlay => SingleGeneration is { Status: "succeeded", VideoPath: { Length: > 0 } };
    public bool HasSingleResult => SingleGeneration != null;
    public string GenerationStage => SingleGeneration?.Stage ?? "";
    public string OutputVoice => SingleGeneration?.Voice.Name ?? "";
    public string OutputCacheSummary => SingleGeneration is { Status: "succeeded" } result
        ? $"复用 {result.CacheHits} 句 · 新配音 {result.Generated} 句"
            + (result.ElapsedSeconds > 0 ? $" · 用时 {result.ElapsedSeconds:0.0} 秒" : "") : "";
    public string OutputDetails => string.Join("；", new[] { SingleGeneration?.Error }
        .Concat(SingleGeneration?.Warnings ?? Array.Empty<string>()).Where(x => !string.IsNullOrWhiteSpace(x)));
    public string OutputFreshness => SingleGeneration is { IsCurrent: false }
        ? "字幕已修改，请重新生成。下方视频为上次生成结果。" : "";
    public Task CurrentOperation { get; private set; } = Task.CompletedTask;
    public string Message { get; private set; } = "添加一个视频开始智能配音。";
    public string EnvironmentMessage { get; private set; } = "正在检查运行环境…";
    public ObservableCollection<DubbingEditableRow> Subtitles { get; } = new();
    public ObservableCollection<DubbingEditableRow> ScriptRows { get; } = new();
    public ObservableCollection<DubbingScript> Scripts { get; } = new();
    public ObservableCollection<DubbingVoiceChoice> Voices { get; } = new();
    public ObservableCollection<DubbingResultRow> Results { get; } = new();
    public DubbingEditableRow? SelectedSubtitle { get; set; }
    public DubbingEditableRow? SelectedScriptRow { get; set; }
    public DubbingResultRow? SelectedResult { get; set; }
    public DubbingScript? SelectedScript { get => _selectedScript; set { _selectedScript = value; DisplayScript(); Notify(); } }
    public bool IsBusy => _busy;
    public bool HasEditError => _editFailed;
    public bool CanEdit => !_busy && _pending == 0;
    public bool CanRecognize => CanEdit && _asrReady && Session is { HasSubtitles: false };
    public bool CanSelectAsr => CanEdit && Session is not { HasSubtitles: true };
    public bool CanNewRecognition => CanEdit && Session != null;
    public IReadOnlyList<DubbingAsrChoice> AsrChoices { get; } = new[] {
        new DubbingAsrChoice("videocaptioner", "卡卡字幕助手（推荐，需联网）"),
        new DubbingAsrChoice("faster-whisper", "Faster-Whisper 本地") };
    private string _asrProvider = "videocaptioner";
    public string AsrProvider { get => _asrProvider; set { if (_asrProvider == value) return; _asrProvider = value;
        _asrReady = false; Notify(); Changed(); if (!_busy) _ = SaveEnvironmentAsync(); } }
    public string SubtitleSource => Session is not { HasSubtitles: true } ? "尚无识别字幕" :
        Session.AsrProvider == "videocaptioner" ? "当前字幕来源：卡卡字幕助手 / B 接口" :
        Session.AsrProvider == "faster-whisper" ? "当前字幕来源：Faster-Whisper" : "当前字幕来源：旧版本地识别项目";
    public bool CanCorrect => CanEdit && Subtitles.Count > 0;
    public bool CanSave => !_busy && Subtitles.Count > 0 && !_editFailed;
    public bool CanVariant => CanEdit && _reviewed && !_draft;
    public bool CanStart => !_busy && !_editFailed && _ttsReady && Session is { HasSubtitles: true } && Subtitles.Count > 0 && SelectedVoice != null && SelectedProvider?.Ready == true && SelectedVoice.Model.Provider == SelectedProvider.Id;
    public int SelectedVoiceCount => Voices.Count(v => v.Selected);
    public int ExpectedCount => SelectedVoice == null ? 0 : 1;
    public string Statistics => "本次生成 1 个配音视频";
    public string VideoName => Session == null ? "尚未添加视频" : Path.GetFileName(Session.SourceVideo);
    public string VideoDuration => Session == null ? "" : $"时长：{Session.DurationMs / 1000.0:0.00} 秒";
    public string ReviewStatus => _editFailed ? "编辑未成功，请修正或放弃修改" : _pending > 0 ? "正在提交编辑…" : _draft ? "有未保存修改" : _reviewed ? "基础字幕已保存并确认" : "请检查字幕；点击生成将自动保存当前文字";

    public string PythonPath { get; set; }
    public string CoreDirectory { get; set; }
    public string FFmpegPath { get; set; }
    public string FFprobePath { get; set; }
    public string CacheDirectory { get; set; }
    public string OutputDirectory { get; set; }
    public string AsrModel { get; set; }
    public string VideoCaptionerCli { get; set; }
    public int AsrTimeoutSeconds { get; set; }

    public VoiceDubbingViewModel(IVoiceDubbingService service)
    {
        _service = service;
        var config = service.Configuration;
        PythonPath = config.PythonPath; CoreDirectory = config.CoreDirectory; FFmpegPath = config.FFmpegPath;
        FFprobePath = config.FFprobePath; CacheDirectory = config.CacheDirectory; OutputDirectory = config.OutputDirectory;
        AsrModel = config.AsrModel;
        _asrProvider = config.AsrProvider; VideoCaptionerCli = config.VideoCaptionerCli; AsrTimeoutSeconds = config.AsrTimeoutSeconds;
        TtsConfigurationPath = config.TtsConfigurationPath;
        TtsConcurrency = config.TtsConcurrency;
        RefreshProviders();
    }

    public event PropertyChangedEventHandler? PropertyChanged;
    private void Notify([CallerMemberName] string? name = null) => PropertyChanged?.Invoke(this, new(name));
    private void Changed() => PropertyChanged?.Invoke(this, new(string.Empty));
    private void Apply(DubbingCorrection state)
    {
        _reviewed = state.Reviewed; _draft = state.HasDraft;
        string? selected = SelectedSubtitle?.Id;
        Subtitles.Clear(); foreach (var row in state.Segments) Subtitles.Add(new(row));
        SelectedSubtitle = Subtitles.FirstOrDefault(r => r.Id == selected);
        Changed();
    }
    private void DisplayScript()
    {
        ScriptRows.Clear();
        foreach (var row in _selectedScript?.Segments ?? Array.Empty<DubbingTextRow>()) ScriptRows.Add(new(row));
        Changed();
    }
    private async Task RefreshScriptsAsync(CancellationToken token)
    {
        string? selected = SelectedScript?.Id;
        var scripts = await _service.ScriptsAsync(Session!, token);
        Scripts.Clear(); foreach (var script in scripts) Scripts.Add(script);
        SelectedScript = Scripts.FirstOrDefault(s => s.Id == selected) ?? Scripts.FirstOrDefault();
        Changed();
    }

    private Task Operate(Func<CancellationToken, Task> action)
    {
        if (_busy) return CurrentOperation;
        _busy = true;
        _operationCancellation = CancellationTokenSource.CreateLinkedTokenSource(_lifetime.Token);
        Changed();
        CurrentOperation = Run();
        return CurrentOperation;
        async Task Run()
        {
            try { await _edits; await action(_operationCancellation.Token); }
            catch (OperationCanceledException) {
                Message = "操作已停止，已有字幕和完成的视频保留。";
                if (Generation != null) ApplyGeneration(Generation with { Status = "cancelled",
                    Combinations = Generation.Combinations.Select(r => r.Status is "waiting" or "running" ? r with { Status = "cancelled" } : r).ToArray() });
            }
            catch (Exception ex) { Message = ex.Message; }
            finally { _busy = false; _operationCancellation.Dispose(); _operationCancellation = null; Changed(); }
        }
    }

    public Task InitializeAsync() => Operate(async token => {
        var environment = await _service.CheckEnvironmentAsync(token);
        _asrReady = environment.AsrReady; _ttsReady = environment.TtsReady; EnvironmentMessage = environment.Summary;
        RefreshProviders();
        await LoadSelectedVoicesAsync(token);
        Session = await _service.RestoreSessionAsync(token);
        if (Session is { HasSubtitles: true }) {
            Apply(await _service.CorrectionAsync(Session, token: token));

        }
        if (Session is { LastSingleGeneration: not null }) {
            SingleGeneration = await _service.ReadSingleGenerationAsync(Session, token); Changed();
        }
        Message = Session == null ? "添加视频，识别后先检查并保存字幕。" : "已恢复最近项目；不会自动启动任务。";
    });

    public Task SaveEnvironmentAsync() => Operate(async token => {
        _service.Configure(_service.Configuration with { PythonPath = PythonPath, CoreDirectory = CoreDirectory,
            FFmpegPath = FFmpegPath, FFprobePath = FFprobePath, CacheDirectory = CacheDirectory,
            OutputDirectory = OutputDirectory, AsrModel = AsrModel, AsrProvider = AsrProvider,
            VideoCaptionerCli = VideoCaptionerCli, AsrTimeoutSeconds = AsrTimeoutSeconds,
            TtsConfigurationPath = TtsConfigurationPath, TtsConcurrency = TtsConcurrency,
            TtsProvider = SelectedProvider?.Id ?? _service.Configuration.TtsProvider });
        var environment = await _service.CheckEnvironmentAsync(token);
        _asrReady = environment.AsrReady; _ttsReady = environment.TtsReady; EnvironmentMessage = environment.Summary;
        RefreshProviders();
        await LoadSelectedVoicesAsync(token);
        Message = "环境设置已保存。";
    });

    public Task AddVideoAsync(string path) => Operate(async token => {
        Session = await _service.AddVideoAsync(path, token);
        Subtitles.Clear(); Scripts.Clear(); ScriptRows.Clear(); Results.Clear(); SelectedScript = null;
        Generation = null; SingleGeneration = null; _reviewed = _draft = _editFailed = false;
        Message = "视频已添加，请识别字幕。";
    });
    public Task RecognizeAsync() => Operate(async token => {
        if (Session == null || Session.HasSubtitles || !_asrReady) throw new InvalidOperationException("请先添加视频并检查识别环境。");
        Message = "正在识别字幕，请稍候…"; Changed();
        Apply(await _service.RecognizeAsync(Session, token));
        Session = Session with { HasSubtitles = true, AsrProvider = AsrProvider };
        Message = "识别完成，可直接编辑字幕；生成时自动保存。";
    });

    public Task NewRecognitionAsync() => Session == null ? Task.CompletedTask : AddVideoAsync(Session.SourceVideo);

    public void QueueCorrectionEdit(string id, string text) => QueueEdit(async () => {
        Apply(await _service.CorrectionAsync(Session!, "set-text", id, text, _lifetime.Token));
        InvalidateOutput();
    });
    public void QueueScriptEdit(string id, string text)
    {
        DubbingScript script = SelectedScript ?? throw new InvalidOperationException("请选择文案版本。");
        QueueEdit(async () => {
            await _service.EditScriptAsync(Session!, script, "set-text", id, text, _lifetime.Token);
            await RefreshScriptsAsync(_lifetime.Token);
        });
    }
    private void QueueEdit(Func<Task> action)
    {
        if (_busy) return;
        Task previous = _edits;
        _pending++; Changed();
        _edits = Run();
        async Task Run()
        {
            try { await Task.Yield(); await previous; await action(); _editFailed = false; Message = "字幕已修改，生成时会自动保存。"; }
            catch (Exception ex) { _editFailed = true; Message = ex.Message; }
            finally { _pending--; Changed(); }
        }
    }

    public Task SaveSubtitlesAsync() => Operate(async token => {
        if (_editFailed) throw new InvalidOperationException("请先修正失败的编辑或放弃修改。");
        Apply(await _service.CorrectionAsync(Session!, "save", token: token));
        Message = "字幕已保存。可选择一个声音生成视频。";
    });
    public Task RestoreSubtitleAsync(bool all) => Operate(async token => {
        if (!all && SelectedSubtitle == null) throw new InvalidOperationException("请选择要恢复的字幕。");
        Apply(await _service.CorrectionAsync(Session!, all ? "restore-all" : "restore", all ? null : SelectedSubtitle?.Id, token: token));
        _editFailed = false; InvalidateOutput(); Message = "已恢复原始识别文字，生成时会自动保存。";
    });
    public Task DiscardAsync() => Operate(async token => {
        Apply(await _service.CorrectionAsync(Session!, "discard", token: token)); _editFailed = false;
        SingleGeneration = await _service.ReadSingleGenerationAsync(Session!, token); Changed(); Message = "已放弃未保存的字幕修改。";
    });
    public Task CreateVariantAsync(string name, bool copy) => Operate(async token => {
        if (!_reviewed || _draft || _editFailed) throw new InvalidOperationException("请先保存并确认基础字幕。");
        if (copy && SelectedScript == null) throw new InvalidOperationException("请选择要复制的版本。");
        var script = await _service.CreateScriptAsync(Session!, name, copy ? SelectedScript : null, token);
        await RefreshScriptsAsync(token); SelectedScript = Scripts.First(s => s.Id == script.Id); Message = "文案版本已建立。";
    });
    public Task RenameVariantAsync(string name) => Operate(async token => {
        if (SelectedScript == null) throw new InvalidOperationException("请选择版本。");
        await _service.EditScriptAsync(Session!, SelectedScript, "rename", text: name, token: token);
        await RefreshScriptsAsync(token); Message = "版本已重命名。";
    });
    public Task DeleteVariantAsync() => Operate(async token => {
        if (SelectedScript == null) throw new InvalidOperationException("请选择版本。");
        await _service.EditScriptAsync(Session!, SelectedScript, "delete", token: token);
        await RefreshScriptsAsync(token); Message = "版本文件已删除，基础字幕和历史视频保留。";
    });
    public Task RestoreVariantTextAsync() => Operate(async token => {
        if (SelectedScript == null || SelectedScriptRow == null) throw new InvalidOperationException("请选择版本中的一句文字。");
        await _service.EditScriptAsync(Session!, SelectedScript, "restore", SelectedScriptRow.Id, token: token);
        await RefreshScriptsAsync(token); _editFailed = false; Message = "该句已恢复为基础字幕文字。";
    });

    private void ApplyGeneration(DubbingGeneration generation)
    {
        Generation = generation;
        string? selected = SelectedResult?.Model.ScriptId + ":" + SelectedResult?.Model.VoiceId;
        Results.Clear(); foreach (var result in generation.Combinations) Results.Add(new(result));
        SelectedResult = Results.FirstOrDefault(r => r.Model.ScriptId + ":" + r.Model.VoiceId == selected);
        Changed();
    }
    public Task StartAdvancedAsync() => Operate(async token => {
        if (!_reviewed || _draft || _editFailed || !_ttsReady || Scripts.Count == 0 || SelectedVoiceCount == 0)
            throw new InvalidOperationException("请确认字幕已保存、版本和声音已选择、TTS 环境可用。");
        var scripts = Scripts.ToArray(); var voices = Voices.Where(v => v.Selected).Select(v => v.Model).ToArray();
        Message = $"正在生成 {scripts.Length * voices.Length} 个视频…"; Changed();
        var result = await _service.GenerateAsync(Session!, scripts, voices, new Progress<DubbingGeneration>(ApplyGeneration), token);
        ApplyGeneration(result);
        Session = Session! with { LastGeneration = result.Directory };
        Message = result.Status switch { "succeeded" => "全部视频已生成，可选择结果播放对比。", "partial_success" => "部分完成，请查看失败组合的错误。", _ => "生成未完成，请查看错误和日志。" };
    });
    private void InvalidateOutput()
    {
        if (SingleGeneration != null) SingleGeneration = SingleGeneration with { IsCurrent = false };
        Changed();
    }
    private void ApplySingle(DubbingSingleGeneration result)
    {
        SingleGeneration = result; Message = result.Stage; Changed();
    }
    public Task StartAsync() => Operate(async token => {
        if (_editFailed || !_ttsReady || Session is not { HasSubtitles: true } || SelectedVoice == null
            || SelectedProvider?.Ready != true || SelectedVoice.Model.Provider != SelectedProvider.Id)
            throw new InvalidOperationException("请先识别字幕、修正失败的编辑并选择一个声音。");
        var voice = SelectedVoice.Model;
        Message = "保存字幕…"; Changed();
        Apply(await _service.CorrectionAsync(Session, "save", token: token));
        if (!_reviewed || _draft) throw new InvalidOperationException("字幕保存未成功，生成已停止。");
        SingleGeneration = null; Changed();
        var result = await _service.GenerateSingleAsync(Session, voice, new Progress<DubbingSingleGeneration>(ApplySingle), token);
        ApplySingle(result);
        Session = Session with { LastSingleGeneration = result.Directory };
        Message = result.Status == "succeeded" ? "新配音视频已生成，可以播放；也可以继续修改另一句再生成。" : "生成未完成，请查看错误和日志。";
    });
    public void Stop() => _operationCancellation?.Cancel();
    public async ValueTask DisposeAsync()
    {
        Stop(); _lifetime.Cancel();
        await CurrentOperation; await _edits; await _service.DisposeAsync(); _lifetime.Dispose();
    }
}
