"""External official CLI boundary. Never imports VideoCaptioner code."""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from ..errors import ProviderError, ValidationError
from ..timeline.srt import load_srt, PARSER_VERSION


def discover_cli() -> Path | None:
    configured = os.environ.get("VIDEOCAPTIONER_CLI")
    if configured:
        return Path(configured)
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "VideoCaptionerCLI/venv/Scripts/videocaptioner.exe"
    if local.is_file():
        return local
    found = shutil.which("videocaptioner")
    return Path(found) if found else None


def _kill_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    else:
        import signal
        os.killpg(process.pid, signal.SIGKILL)


class VideoCaptionerASRProvider:
    provider_id = "videocaptioner"

    def __init__(self, *, output_dir: Path, cli_path: Path | None = None,
                 timeout: float = 600, ffmpeg_path: Path | None = None,
                 ffprobe_path: Path | None = None):
        self.cli_path = cli_path or discover_cli()
        if not self.cli_path or not self.cli_path.is_file():
            raise ProviderError("VideoCaptioner CLI not found; configure --videocaptioner-cli (GUI 1.3.3 is not a CLI)")
        if not 0 < timeout < float("inf"):
            raise ValidationError("ASR timeout must be a positive finite number")
        self.output_dir = output_dir.resolve()
        self.timeout = timeout
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        metadata = self.cli_path.parent.parent / "Lib/site-packages"
        versions = sorted(p.name for p in metadata.glob("videocaptioner-*.dist-info"))
        self.cache_identity = {"engine": "bijian", "tool_version": versions,
            "launcher_sha256": hashlib.sha256(self.cli_path.read_bytes()).hexdigest(), "parser_version": PARSER_VERSION}

    def transcribe(self, audio_path: Path, *, language: str | None = None):
        return self.transcribe_video(audio_path, language=language)

    def transcribe_video(self, video_path: Path, *, language: str | None = None):
        if not video_path.is_file():
            raise ProviderError(f"ASR input not found: {video_path}")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        target = self.output_dir / "subtitle.srt"
        if target.exists():
            raise ProviderError(f"refusing existing subtitle output: {target}")
        logs = self.output_dir / "asr_logs"
        temp = self.output_dir / "asr_tmp"
        logs.mkdir(); temp.mkdir()
        config = logs / "captioner.toml"
        config.write_text('[transcribe]\nasr = "bijian"\n[output]\nformat = "srt"\n', encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("VIDEOCAPTIONER_")}
        for secret in ("ELEVENLABS_API_KEY", "NOIZ_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
            env.pop(secret, None)
        env.update({"TMP": str(temp), "TEMP": str(temp), "TMPDIR": str(temp), "PYTHONUTF8": "1"})
        profile = temp / "profile"
        profile.mkdir()
        # Public platformdirs overrides keep the external tool's own cache/logs
        # within this attempt too, without changing its package or GUI settings.
        env.update({"USERPROFILE": str(profile), "WIN_PD_OVERRIDE_LOCAL_APPDATA": str(profile / "AppData/Local"),
                    "WIN_PD_OVERRIDE_APPDATA": str(profile / "AppData/Roaming"),
                    "XDG_DATA_HOME": str(profile / "data"), "XDG_CACHE_HOME": str(profile / "cache")})
        binaries = [str(p.resolve().parent) for p in (self.ffmpeg_path, self.ffprobe_path) if p]
        env["PATH"] = os.pathsep.join(binaries + [env.get("PATH", "")])
        command = [str(self.cli_path.resolve()), "transcribe", str(video_path.resolve()),
            "--asr", "bijian", "--language", language or "auto", "--format", "srt",
            "--config", str(config), "-o", str(target)]
        options = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True})
        try:
            with (logs / "stdout.log").open("wb") as stdout, (logs / "stderr.log").open("wb") as stderr:
                process = subprocess.Popen(command, cwd=self.output_dir, env=env, stdout=stdout, stderr=stderr, **options)
                try:
                    code = process.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired as exc:
                    _kill_tree(process)
                    process.wait(timeout=15)
                    raise ProviderError(f"VideoCaptioner timed out after {self.timeout}s; logs: {logs}") from exc
                except (KeyboardInterrupt, SystemExit):
                    _kill_tree(process)
                    process.wait(timeout=15)
                    raise
            if code:
                raise ProviderError(f"VideoCaptioner failed (exit {code}); logs: {logs}; select Faster-Whisper to retry")
            return load_srt(target)
        except (OSError, ValidationError) as exc:
            raise ProviderError(f"VideoCaptioner produced no usable subtitles: {exc}; logs: {logs}") from exc
