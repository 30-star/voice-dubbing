using GaoxiaoVideo.Services;
using Microsoft.Win32;
using System;
using System.IO;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;

namespace GaoxiaoVideo.Views;

public partial class VoiceDubbingPage : UserControl
{
    private bool _initialized;
    public VoiceDubbingViewModel ViewModel { get; }
    // Injectable UI interaction points let acceptance exercise actual routed button/edit events.
    public Func<string?> PickVideo { get; set; } = SelectVideo;
    public Func<string, string?> PromptName { get; set; } = AskName;
    public Action<string> OpenResult { get; set; } = VoiceDubbingShell.Open;
    public VoiceDubbingPage() : this(new VoiceDubbingService()) { }
    public VoiceDubbingPage(IVoiceDubbingService service)
    {
        InitializeComponent(); ViewModel = new(service); DataContext = ViewModel;
        Loaded += async (_, _) => { if (_initialized) return; _initialized = true; await ViewModel.InitializeAsync(); };
    }
    private static string? SelectVideo()
    {
        var dialog = new OpenFileDialog { Title = "添加一个配音视频", Multiselect = false,
            Filter = "视频文件|*.mp4;*.mov;*.mkv;*.avi;*.webm|所有文件|*.*" };
        return dialog.ShowDialog() == true ? dialog.FileName : null;
    }
    private static string? AskName(string initial)
    {
        var input = new TextBox { Text = initial, Margin = new Thickness(12), MinWidth = 240 };
        var button = new Button { Content = "确定", IsDefault = true, Margin = new Thickness(12), Padding = new Thickness(15, 6, 15, 6), HorizontalAlignment = HorizontalAlignment.Right };
        var panel = new StackPanel(); panel.Children.Add(input); panel.Children.Add(button);
        var window = new Window { Title = "文案版本名称", Content = panel, SizeToContent = SizeToContent.WidthAndHeight,
            ResizeMode = ResizeMode.NoResize, WindowStartupLocation = WindowStartupLocation.CenterOwner,
            Owner = Application.Current?.MainWindow };
        button.Click += (_, _) => { if (!string.IsNullOrWhiteSpace(input.Text)) window.DialogResult = true; };
        window.Loaded += (_, _) => { input.Focus(); input.SelectAll(); };
        return window.ShowDialog() == true ? input.Text : null;
    }
    private bool CommitEditors() => SubtitleGrid.CommitEdit(DataGridEditingUnit.Cell, true)
        && SubtitleGrid.CommitEdit(DataGridEditingUnit.Row, true);
    private async void AddVideo_Click(object sender, RoutedEventArgs e)
    {
        if (!CommitEditors()) return;
        if (ViewModel.ReviewStatus == "有未保存修改" && MessageBox.Show(Window.GetWindow(this),
            "当前字幕草稿会保留在原项目中，是否添加另一个视频？", "智能配音", MessageBoxButton.YesNo) != MessageBoxResult.Yes) return;
        string? path = PickVideo(); if (path != null) await ViewModel.AddVideoAsync(path);
    }
    private async void Recognize_Click(object sender, RoutedEventArgs e) => await ViewModel.RecognizeAsync();
    private async void NewRecognition_Click(object sender, RoutedEventArgs e) {
        if (CommitEditors()) await ViewModel.NewRecognitionAsync();
    }
    private async void SaveSubtitles_Click(object sender, RoutedEventArgs e) { if (CommitEditors()) await ViewModel.SaveSubtitlesAsync(); }
    private async void RestoreSubtitle_Click(object sender, RoutedEventArgs e) { if (CommitEditors()) await ViewModel.RestoreSubtitleAsync(false); }
    private async void RestoreAll_Click(object sender, RoutedEventArgs e) { if (CommitEditors()) await ViewModel.RestoreSubtitleAsync(true); }
    private async void Discard_Click(object sender, RoutedEventArgs e) {
        SubtitleGrid.CancelEdit(DataGridEditingUnit.Cell); SubtitleGrid.CancelEdit(DataGridEditingUnit.Row);
        await ViewModel.DiscardAsync();
    }
    private async void NewVariant_Click(object sender, RoutedEventArgs e) { if (!CommitEditors()) return; string? name = PromptName("新版本"); if (name != null) await ViewModel.CreateVariantAsync(name, false); }
    private async void CopyVariant_Click(object sender, RoutedEventArgs e) { if (!CommitEditors()) return; string? name = PromptName("版本副本"); if (name != null) await ViewModel.CreateVariantAsync(name, true); }
    private async void RenameVariant_Click(object sender, RoutedEventArgs e) { if (!CommitEditors()) return; string? name = PromptName(ViewModel.SelectedScript?.Name ?? "版本"); if (name != null) await ViewModel.RenameVariantAsync(name); }
    private async void DeleteVariant_Click(object sender, RoutedEventArgs e) { if (!CommitEditors()) return; if (MessageBox.Show(Window.GetWindow(this), "仅删除所选文案版本，基础字幕和历史视频保留。", "删除版本", MessageBoxButton.YesNo) == MessageBoxResult.Yes) await ViewModel.DeleteVariantAsync(); }
    private async void RestoreVariant_Click(object sender, RoutedEventArgs e) { if (CommitEditors()) await ViewModel.RestoreVariantTextAsync(); }
    private async void Start_Click(object sender, RoutedEventArgs e) { if (CommitEditors()) await ViewModel.StartAsync(); }
    private void Stop_Click(object sender, RoutedEventArgs e) => ViewModel.Stop();
    private async void Environment_Click(object sender, RoutedEventArgs e) => await ViewModel.SaveEnvironmentAsync();
    private void Subtitle_CellEditEnding(object sender, DataGridCellEditEndingEventArgs e)
    {
        if (e.EditAction == DataGridEditAction.Commit && e.Row.Item is DubbingEditableRow row && e.EditingElement is TextBox text
            && (text.Text != row.Model.Text || ViewModel.HasEditError))
            ViewModel.QueueCorrectionEdit(row.Id, text.Text);
    }
    private void Variant_CellEditEnding(object sender, DataGridCellEditEndingEventArgs e)
    {
        if (e.EditAction == DataGridEditAction.Commit && e.Row.Item is DubbingEditableRow row && e.EditingElement is TextBox text
            && (text.Text != row.Model.Text || ViewModel.HasEditError))
            ViewModel.QueueScriptEdit(row.Id, text.Text);
    }
    private void Open(string? path)
    {
        try { if (string.IsNullOrEmpty(path)) throw new IOException("请先选择一个已完成结果。"); OpenResult(path); }
        catch (Exception ex) { MessageBox.Show(Window.GetWindow(this), ex.Message, "智能配音"); }
    }
    private void Play_Click(object sender, RoutedEventArgs e) => Open(ViewModel.OutputPath);
    private void OpenDirectory_Click(object sender, RoutedEventArgs e) => Open(ViewModel.SingleGeneration?.Directory ?? ViewModel.Session?.Directory);
    private void OpenReport_Click(object sender, RoutedEventArgs e) => Open(ViewModel.SingleGeneration?.AudioPath is { Length: > 0 } audio
        ? Path.Combine(Path.GetDirectoryName(audio)!, "dubbing_report.json")
        : ViewModel.SingleGeneration == null ? null : Path.Combine(ViewModel.SingleGeneration.Directory, "batch_report.json"));
    public async Task ShutdownAsync() => await ViewModel.DisposeAsync();
}
