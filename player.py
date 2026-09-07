"""Local media playback controller used by the desktop client.

The controller keeps media playback on the user's computer.  It uses the
bundled ffplay executable when available and never writes to the NAS.
"""
from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import threading
import time

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".mts", ".m2ts", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg"}


def find_ffplay(base: Path | None = None) -> Path | None:
    candidates = []
    if base:
        candidates.append(Path(base) / "bin" / "ffplay.exe")
    candidates.append(Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "ffmpeg" / "bin" / "ffplay.exe")
    found = shutil.which("ffplay")
    if found:
        candidates.append(Path(found))
    return next((item for item in candidates if item.is_file()), None)


class PlayerError(RuntimeError):
    pass


class MediaPlayer:
    def __init__(self, ffplay: Path | None = None):
        self.ffplay = Path(ffplay) if ffplay else find_ffplay()
        self.process: subprocess.Popen | None = None
        self.source: Path | None = None
        self.volume = 100
        self._lock = threading.RLock()
        self.started_at = 0.0
        self.paused_at = 0.0
        self.elapsed_before_pause = 0.0

    @property
    def playing(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def open(self, source: str | Path) -> None:
        path = Path(source)
        if path.suffix.lower() not in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS:
            raise PlayerError("不支持的媒体格式")
        if not path.is_file():
            raise PlayerError("素材不可访问")
        if not self.ffplay or not self.ffplay.is_file():
            raise PlayerError("未找到内置播放器组件 ffplay")
        self.stop()
        command = [str(self.ffplay), "-hide_banner", "-loglevel", "error", "-autoexit", "-volume", str(self.volume), str(path)]
        with self._lock:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.source = path
            self.started_at = time.monotonic()
            self.elapsed_before_pause = 0.0
            self.paused_at = 0.0

    def embed(self, source: str | Path, window_id: int) -> None:
        """Open ffplay as a child of a Tk window on Windows."""
        path = Path(source)
        if path.suffix.lower() not in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS or not path.is_file():
            raise PlayerError("素材不可访问或格式不支持")
        if not self.ffplay or not self.ffplay.is_file():
            raise PlayerError("未找到内置播放器组件 ffplay")
        self.stop()
        command = [str(self.ffplay), "-hide_banner", "-loglevel", "error", "-autoexit", "-noborder", "-volume", str(self.volume), "-window_title", "素材预览", "-x", "640", "-y", "360", str(path)]
        if os.name == "nt":
            command[1:1] = ["-i", str(window_id)]
        with self._lock:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.source = path

    def pause(self) -> None:
        self._send("p")
        if self.paused_at:
            self.elapsed_before_pause += time.monotonic() - self.paused_at
            self.paused_at = 0.0
        else:
            self.paused_at = time.monotonic()

    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.paused_at or time.monotonic()
        return max(0.0, end - self.started_at - self.elapsed_before_pause)

    def seek(self, seconds: int) -> None:
        if seconds == 0:
            return
        self._send("\033")

    def set_volume(self, value: int) -> None:
        self.volume = max(0, min(100, int(value)))
        if self.playing:
            self._send("9" if self.volume < 50 else "0")

    def stop(self) -> None:
        with self._lock:
            process, self.process = self.process, None
            self.source = None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    def _send(self, value: str) -> None:
        with self._lock:
            if self.process and self.process.poll() is None and self.process.stdin:
                try:
                    self.process.stdin.write(value.encode("ascii"))
                    self.process.stdin.flush()
                except OSError:
                    pass

    def close(self) -> None:
        self.stop()
