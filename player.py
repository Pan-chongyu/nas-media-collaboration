"""Local mpv playback with asynchronous JSON IPC and native Windows embedding.

Only the mpv worker touches the media path. Tk reads cached playback state,
so an unavailable SMB file never blocks the desktop event loop.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".mts", ".m2ts", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg"}


def find_mpv(base: Path | None = None) -> Path | None:
    roots = [Path(base)] if base else []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).parent)
    if getattr(sys, "_MEIPASS", None):
        roots.append(Path(sys._MEIPASS))
    roots.append(Path(__file__).resolve().parent)
    candidates = [root / suffix for root in roots for suffix in
                  ("tools/mpv/mpv.exe", "tools/mpv.exe", "bin/mpv.exe", "mpv.exe")]
    if os.getenv("MPV_PATH"):
        candidates.insert(0, Path(os.environ["MPV_PATH"]))
    candidates.append(Path(os.getenv("LOCALAPPDATA", "")) / "Programs/mpv/mpv.exe")
    if not getattr(sys, "frozen", False):
        candidates.append(Path(__file__).resolve().parent / "build/dependencies/mpv/mpv.exe")
    found = shutil.which("mpv")
    if found:
        candidates.append(Path(found))
    return next((path for path in candidates if path.is_file()), None)


class PlayerError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlaybackState:
    active: bool = False
    loading: bool = False
    paused: bool = False
    ended: bool = False
    elapsed: float = 0.0
    duration: float = 0.0
    volume: float = 80.0
    error: str = ""


class _WindowsIPC:
    """Nonblocking reads from mpv's local named pipe; used only by its worker."""
    def __init__(self, path: str):
        import msvcrt
        self.stream = open(path, "r+b", buffering=0)
        self.handle = wintypes.HANDLE(msvcrt.get_osfhandle(self.stream.fileno()))
        self.pending = b""
        self.peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
        self.peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                              ctypes.POINTER(wintypes.DWORD)]
        self.peek.restype = wintypes.BOOL

    def send(self, command, request_id=0):
        payload = (json.dumps({"command": command, "request_id": request_id}, ensure_ascii=False) + "\n").encode("utf-8")
        self.stream.write(payload)

    def read(self):
        available = wintypes.DWORD()
        if not self.peek(self.handle, None, 0, None, ctypes.byref(available), None):
            raise OSError(ctypes.get_last_error(), "播放器连接已关闭")
        if available.value:
            self.pending += self.stream.read(min(available.value, 1024 * 1024))
        messages = []
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            if line:
                messages.append(json.loads(line))
        return messages

    def close(self):
        self.stream.close()


class _WindowsVideo:
    """Move the same mpv child window between preview and fullscreen hosts."""
    def __init__(self, pid: int, initial_parent: int):
        self.pid, self.parent, self.hwnd = pid, initial_parent, 0
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetParent.argtypes = [wintypes.HWND]
        self.user32.GetParent.restype = wintypes.HWND
        self.user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
        self.user32.SetParent.restype = wintypes.HWND
        self.user32.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL]
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user32.EnumChildWindows.argtypes = [wintypes.HWND, self.callback_type, wintypes.LPARAM]
        self.last_size = None

    def attach(self, parent: int, width: int, height: int):
        if not self.hwnd:
            @self.callback_type
            def visit(hwnd, _):
                pid = wintypes.DWORD()
                self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == self.pid:
                    self.hwnd = hwnd
                    return False
                return True
            self.user32.EnumChildWindows(self.parent, visit, 0)
        if not self.hwnd or not self.user32.IsWindow(parent):
            return
        if self.parent != parent:
            self.user32.SetParent(self.hwnd, parent)
            self.parent = parent
            self.last_size = None
        size = (max(1, width), max(1, height))
        if self.last_size != size:
            self.user32.MoveWindow(self.hwnd, 0, 0, *size, True)
            self.last_size = size


class MediaPlayer:
    def __init__(self, mpv: Path | None = None):
        self.mpv = Path(mpv) if mpv else find_mpv()
        self.process: subprocess.Popen | None = None
        self.source: Path | None = None
        self.volume = 80
        self._lock = threading.RLock()
        self._state = PlaybackState()
        self._generation = 0
        self._cancel = threading.Event()
        self._commands: queue.Queue = queue.Queue()
        self._ui_events: queue.Queue = queue.Queue()
        self._target = (0, 1, 1)
        self._thread: threading.Thread | None = None
        self._video = None

    @property
    def state(self) -> PlaybackState:
        with self._lock:
            return self._state

    @property
    def playing(self) -> bool:
        return self.state.active

    @property
    def elapsed(self) -> float:
        return self.state.elapsed

    def _update(self, generation, **changes):
        with self._lock:
            if generation == self._generation:
                self._state = replace(self._state, **changes)

    def open(self, source: str | Path, window_id: int = 0) -> None:
        path = Path(source)
        if path.suffix.lower() not in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS:
            raise PlayerError("不支持的媒体格式")
        if os.name != "nt":
            raise PlayerError("内置播放器需要 Windows")
        if not self.mpv:
            raise PlayerError("未找到内置播放器组件 mpv，请重新安装软件")
        self.stop()
        with self._lock:
            self.source = path
            self._state = PlaybackState(active=True, loading=True, volume=self.volume)
            self._cancel = threading.Event()
            self._commands = queue.Queue()
            self._ui_events = queue.Queue()
            self._target = (int(window_id), 248, 140)
            generation = self._generation
            self._thread = threading.Thread(target=self._run,
                args=(generation, path, int(window_id), self._cancel, self._commands),
                daemon=True, name="media-player")
            self._thread.start()

    def embed(self, source: str | Path, window_id: int) -> None:
        self.open(source, window_id)

    def attach(self, window_id: int, width: int, height: int):
        with self._lock:
            self._target = (int(window_id), int(width), int(height))
            if self._video:
                self._video.attach(*self._target)

    def drain_events(self):
        events = []
        while True:
            try:
                events.append(self._ui_events.get_nowait())
            except queue.Empty:
                return events

    def pause(self) -> None:
        if self.playing:
            self._commands.put(["cycle", "pause"])

    def seek(self, seconds: float) -> None:
        state = self.state
        if state.active and state.duration > 0:
            self._commands.put(["seek", max(0.0, min(float(seconds), state.duration)), "absolute+exact"])

    def set_volume(self, value: int) -> None:
        self.volume = max(0, min(100, int(value)))
        if self.playing:
            self._commands.put(["set_property", "volume", self.volume])

    def stop(self) -> None:
        with self._lock:
            process = self.process
            self._generation += 1
            self._cancel.set()
            self.source = None
            self.process = None
            self._video = None
            self._state = PlaybackState(volume=self.volume)
        # Send termination before the interpreter can exit and stop daemon workers.
        # TerminateProcess does not wait for media IO; the worker reaps the process.
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def close(self) -> None:
        self.stop()

    def _event(self, generation, event):
        kind = event.get("event")
        if kind == "property-change":
            name, value = event.get("name"), event.get("data")
            if name in {"time-pos", "duration", "volume"} and isinstance(value, (int, float)):
                self._update(generation, **{{"time-pos": "elapsed"}.get(name, name): max(0.0, float(value))})
            elif name == "pause" and isinstance(value, bool):
                self._update(generation, paused=value)
            elif name == "eof-reached" and isinstance(value, bool):
                self._update(generation, ended=value)
            elif name == "paused-for-cache" and isinstance(value, bool):
                self._update(generation, loading=value)
        elif kind == "client-message":
            arguments = event.get("args", [])
            if arguments and arguments[0] in {"nas-exit-fullscreen", "nas-toggle-fullscreen"}:
                with self._lock:
                    if generation == self._generation:
                        self._ui_events.put(arguments[0])
        elif kind == "file-loaded":
            self._update(generation, loading=False, ended=False, error="")
        elif kind == "end-file" and event.get("reason") == "error":
            self._update(generation, active=False, loading=False, error="无法播放素材，请检查 NAS 连接或媒体文件")
        elif kind == "end-file" and event.get("reason") == "eof":
            self._update(generation, ended=True, loading=False)

    def _run(self, generation, source, parent, cancel, commands):
        process = ipc = None
        try:
            pipe = r"\\.\pipe\nas-media-" + uuid.uuid4().hex
            command = [str(self.mpv), "--no-config", "--load-scripts=no", "--idle=yes",
                       "--keep-open=yes", "--force-window=yes", "--terminal=no", "--osc=no",
                       "--input-default-bindings=no", "--input-vo-keyboard=yes",
                       "--input-terminal=no", "--hwdec=auto-safe", "--volume=" + str(self.volume),
                       "--input-ipc-server=" + pipe]
            if parent:
                command.append("--wid=" + str(parent))
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with self._lock:
                if generation == self._generation:
                    self.process = process
            deadline = time.monotonic() + 12
            while not cancel.is_set():
                if process.poll() is not None:
                    raise PlayerError("播放器组件启动失败，请重新安装软件")
                try:
                    ipc = _WindowsIPC(pipe)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise PlayerError("播放器组件连接超时")
                    cancel.wait(0.05)
            if cancel.is_set():
                return
            for index, prop in enumerate(("time-pos", "duration", "pause", "eof-reached", "volume", "paused-for-cache"), 1):
                ipc.send(["observe_property", index, prop])
            for key, binding in (("ESC", "script-message nas-exit-fullscreen"),
                                 ("f", "script-message nas-toggle-fullscreen"),
                                 ("MBTN_LEFT_DBL", "script-message nas-toggle-fullscreen"),
                                 ("LEFT", "seek -5 relative+exact"),
                                 ("RIGHT", "seek 5 relative+exact"),
                                 ("SPACE", "cycle pause")):
                ipc.send(["keybind", key, binding])
            ipc.send(["loadfile", str(source), "replace"])
            video = _WindowsVideo(process.pid, parent) if parent else None
            with self._lock:
                if generation == self._generation:
                    self._video = video
            while not cancel.is_set():
                if process.poll() is not None:
                    raise PlayerError("播放器已退出")
                for event in ipc.read():
                    self._event(generation, event)
                for _ in range(30):
                    try:
                        ipc.send(commands.get_nowait())
                    except queue.Empty:
                        break
                if video:
                    with self._lock:
                        if generation == self._generation:
                            video.attach(*self._target)
                cancel.wait(0.03)
        except (OSError, ValueError, PlayerError) as exc:
            self._update(generation, active=False, loading=False, error=str(exc))
        finally:
            if ipc:
                ipc.close()
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            with self._lock:
                if generation == self._generation:
                    self.process = None
                    self._video = None
