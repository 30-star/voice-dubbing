using System.Diagnostics;
using System.IO;

namespace GaoxiaoVideo.Services;

public static class VoiceDubbingShell
{
    public static void Open(string path)
    {
        if (!File.Exists(path) && !Directory.Exists(path)) throw new IOException("选中的结果尚不存在。");
        Process.Start(new ProcessStartInfo(Path.GetFullPath(path)) { UseShellExecute = true });
    }
}
