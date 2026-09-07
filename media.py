"""Media inspection and thumbnail generation helpers.

The functions in this module deliberately have no Tkinter or application state
dependencies.  They can therefore be called from a worker thread/process by
the desktop client while the UI remains responsive.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

try:  # Pillow is optional at import time for a graceful application start.
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover - exercised when Pillow is not installed
    Image = None  # type: ignore[assignment,misc]
    ImageOps = None  # type: ignore[assignment,misc]


BACKEND_VERSION = "thumb-v1"
DEFAULT_SIZE = (640, 360)
_CACHE_LOCKS = [threading.Lock() for _ in range(32)]

_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".webp", ".bmp", ".gif", ".tif",
    ".tiff", ".ico", ".heic", ".heif", ".avif",
}
_VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".flv",
    ".ts", ".mts", ".m2ts", ".3gp", ".mpeg", ".mpg",
}
_AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus",
    ".wma", ".aiff", ".aif",
}


def media_kind(path: str | os.PathLike[str]) -> str:
    """Return a display label for a media path."""

    suffix = Path(path).suffix.lower()
    if suffix in _VIDEO_EXTENSIONS:
        return "视频"
    if suffix in _IMAGE_EXTENSIONS:
        return "图片"
    if suffix in _AUDIO_EXTENSIONS:
        return "音频"
    return "文件"


def _ffmpeg_path() -> str | None:
    """Find the bundled executable before looking at the host PATH."""

    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        base = Path(meipass)
        candidates.extend((base / "tools" / "ffmpeg.exe", base / "ffmpeg.exe"))
    executable = shutil.which("ffmpeg")
    if executable:
        candidates.append(Path(executable))
    local_app_data = os.environ.get("LOCALAPPDATA")
    program_files = os.environ.get("ProgramFiles")
    for root in (local_app_data, program_files, os.environ.get("ProgramW6432")):
        if root:
            candidates.append(Path(root) / "ffmpeg" / "bin" / "ffmpeg.exe")
            candidates.append(Path(root) / "ffmpeg" / "ffmpeg.exe")
    candidates.extend((Path(r"C:\ffmpeg\bin\ffmpeg.exe"), Path(r"C:\ProgramData\chocolatey\bin\ffmpeg.exe")))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def _run_ffmpeg(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=flags,
        check=False,
    )


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2})(?:\.(\d+))?")
_DIMENSIONS_RE = re.compile(r"(?<!\d)(\d{2,5})x(\d{2,5})(?!\d)")


def _parse_metadata(stderr: str) -> tuple[str, int, int]:
    duration_match = _DURATION_RE.search(stderr)
    duration = ""
    if duration_match:
        duration = _format_duration(
            int(duration_match.group(1)) * 3600
            + int(duration_match.group(2)) * 60
            + int(duration_match.group(3))
            + (float(f"0.{duration_match.group(4)}") if duration_match.group(4) else 0)
        )
    width = height = 0
    video_index = stderr.find("Video:")
    dimension_match = _DIMENSIONS_RE.search(stderr[video_index:] if video_index >= 0 else stderr)
    if dimension_match:
        width, height = int(dimension_match.group(1)), int(dimension_match.group(2))
    return duration, width, height


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds + 0.5))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _cache_key(path: Path, size: tuple[int, int], stat: os.stat_result) -> str:
    normalized = os.path.normcase(os.path.abspath(str(path)))
    payload = {
        "path": normalized,
        "size": [int(size[0]), int(size[1])],
        "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
        "bytes": int(stat.st_size),
        "backend": BACKEND_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _publish_jpeg(image: Any, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.stem}.", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        image.save(tmp_path, format="JPEG", quality=88, optimize=True)
        os.replace(tmp_path, destination)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _image_thumbnail(source: Path, destination: Path, size: tuple[int, int]) -> tuple[int, int]:
    if Image is None or ImageOps is None:
        raise RuntimeError("Pillow 未安装")
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        width, height = image.size
        target_w, target_h = size
        scale = min(target_w / width, target_h / height)
        fitted = image.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (target_w, target_h), (28, 34, 42))
        canvas.paste(fitted, ((target_w - fitted.width) // 2, (target_h - fitted.height) // 2))
        _publish_jpeg(canvas, destination)
    return width, height


def _video_thumbnail(source: Path, destination: Path, size: tuple[int, int], ffmpeg: str) -> tuple[str, int, int]:
    target_w, target_h = size
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.stem}.", suffix=".jpg.tmp", dir=destination.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    filter_spec = f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=1c222a"
    metadata = ""
    width = height = 0
    try:
        for seek in ("1", "0"):
            command = [ffmpeg, "-hide_banner", "-loglevel", "info", "-threads", "1", "-y", "-ss", seek,
                       "-i", str(source), "-frames:v", "1", "-vf", filter_spec, "-q:v", "3", "-f", "image2", str(tmp_path)]
            try:
                completed = _run_ffmpeg(command, timeout=25)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("视频抽帧超时") from exc
            metadata = completed.stderr
            if completed.returncode == 0 and tmp_path.exists() and tmp_path.stat().st_size > 0:
                os.replace(tmp_path, destination)
                duration, width, height = _parse_metadata(metadata)
                return duration, width, height
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
        duration, width, height = _parse_metadata(metadata)
        detail = metadata.strip().splitlines()[-1] if metadata.strip() else "FFmpeg 抽帧失败"
        raise RuntimeError(detail[:240])
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def thumbnail(path: str | os.PathLike[str], cache_dir: Path, size: tuple[int, int] = DEFAULT_SIZE) -> dict[str, Any]:
    """Create or retrieve a cached thumbnail and lightweight media metadata."""

    result: dict[str, Any] = {"path": "", "duration": "", "width": 0, "height": 0, "error": ""}
    source = Path(path)
    try:
        stat = source.stat()
    except (OSError, ValueError) as exc:
        result["error"] = f"文件不可访问: {exc}"
        return result
    if not source.is_file():
        result["error"] = "路径不是文件"
        return result
    kind = media_kind(source)
    if kind == "文件":
        result["error"] = "不支持的媒体格式"
        return result
    if kind == "音频":
        ffmpeg = _ffmpeg_path()
        if ffmpeg:
            try:
                probe = _run_ffmpeg([ffmpeg, "-hide_banner", "-i", str(source)], timeout=10)
                result["duration"], _, _ = _parse_metadata(probe.stderr)
            except (OSError, subprocess.TimeoutExpired):
                pass
        return result
    try:
        target_size = (int(size[0]), int(size[1]))
        if target_size[0] <= 0 or target_size[1] <= 0:
            raise ValueError("缩略图尺寸必须为正数")
        cache_root = Path(cache_dir)
        destination = cache_root / f"{_cache_key(source, target_size, stat)}.jpg"
        if destination.is_file() and destination.stat().st_size > 0:
            if Image is not None:
                try:
                    with Image.open(destination) as cached:
                        cached.verify()
                except (OSError, ValueError):
                    destination.unlink(missing_ok=True)
                else:
                    result["path"] = str(destination)
            else:
                result["path"] = str(destination)
            if result["path"]:
                if kind == "图片" and Image is not None:
                    with Image.open(source) as image:
                        result["width"], result["height"] = ImageOps.exif_transpose(image).size
                return result
        if kind == "图片":
            result["width"], result["height"] = _image_thumbnail(source, destination, target_size)
        else:
            ffmpeg = _ffmpeg_path()
            if not ffmpeg:
                raise RuntimeError("未找到 FFmpeg")
            result["duration"], result["width"], result["height"] = _video_thumbnail(source, destination, target_size, ffmpeg)
        result["path"] = str(destination)
    except (OSError, ValueError, RuntimeError) as exc:
        result["error"] = str(exc)
    return result


def generate_thumbnail(source: str | os.PathLike[str], cache_dir: Path, size: tuple[int, int] = DEFAULT_SIZE, *, force: bool = False) -> Path | None:
    lock = _CACHE_LOCKS[hash(os.path.normcase(str(source))) % len(_CACHE_LOCKS)]
    with lock:
        result = thumbnail(source, cache_dir, size)
    value = result.get("path") if isinstance(result, dict) else None
    return Path(value) if value else None

def _ffmpeg() -> str | None:
    return _ffmpeg_path()

__all__ = ["media_kind", "thumbnail", "generate_thumbnail", "_ffmpeg", "_ffmpeg_path"]
