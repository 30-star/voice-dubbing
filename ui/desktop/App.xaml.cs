using System;
using System.IO;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using GaoxiaoVideo.Views;

namespace VoiceDubbing.Desktop;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        var page = new VoiceDubbingPage();
        var window = new Window { Title = "智能配音 · voice-dubbing", Width = 1150, Height = 800,
            MinWidth = 800, MinHeight = 580, Content = page };
        MainWindow = window;
        bool closing = false;
        bool stopping = false;
        window.Closing += async (_, args) => {
            if (closing) return;
            args.Cancel = true;
            if (stopping) return;
            stopping = true;
            await page.ShutdownAsync();
            closing = true;
            _ = window.Dispatcher.BeginInvoke(new Action(window.Close));
        };
        window.Show();
        // Explicit offline visual smoke check; never starts ASR or TTS.
        if (e.Args.Length == 2 && e.Args[0] == "--render-check")
        {
            window.UpdateLayout();
            var bitmap = new RenderTargetBitmap((int)window.ActualWidth, (int)window.ActualHeight,
                96, 96, PixelFormats.Pbgra32);
            bitmap.Render(window);
            var encoder = new PngBitmapEncoder();
            encoder.Frames.Add(BitmapFrame.Create(bitmap));
            using (var file = File.Create(Path.GetFullPath(e.Args[1]))) encoder.Save(file);
            window.Close();
        }
    }
}
