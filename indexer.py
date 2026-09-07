"""Streaming read-only media inventory and per-client SQLite index."""

from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
from typing import Iterable


VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".avi", ".mxf", ".webm", ".wmv", ".flv", ".ts", ".mts", ".m2ts", ".3gp"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic", ".heif"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma", ".aiff", ".aif", ".ape"})
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS
SKIP_DIRECTORY_NAMES = frozenset({".git", ".cache", "__pycache__", "cache", "tmp", "temp", "temporary", "thumbnail", "thumbnails", ".thumbnails", "thumb", "thumbs", "proxy", "proxies", ".proxy", "_proxy", "代理", "缩略图", "events", "snapshots"})


def canonical_root(value: str | os.PathLike) -> str:
    if not value or not str(value).strip():
        raise ValueError("素材根目录不能为空")
    value = os.path.abspath(os.path.normpath(os.path.expanduser(str(value))))
    # Resolve a mapped drive with the Windows mapping table, not NAS file I/O.
    if os.name == "nt" and len(value) >= 2 and value[1] == ":":
        import ctypes
        from ctypes import wintypes

        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if ctypes.windll.mpr.WNetGetConnectionW(value[:2], buffer, ctypes.byref(length)) == 0:
            value = buffer.value + value[2:]
    return value


def root_key(root: str | os.PathLike) -> str:
    canonical = canonical_root(root).replace("\\", "/").rstrip("/").casefold()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AssetRecord:
    asset_id: str
    root_key: str
    root: str
    path: str
    relative_path: str
    name: str
    extension: str
    media_type: str
    size: int
    mtime_ns: int
    file_hash: str

    @property
    def hash(self):
        return self.file_hash


def _media_type(extension: str) -> str:
    return "video" if extension in VIDEO_EXTENSIONS else "image" if extension in IMAGE_EXTENSIONS else "audio" if extension in AUDIO_EXTENSIONS else "other"


def _record(root: str, path: Path, size: int, mtime_ns: int) -> AssetRecord:
    relative = path.relative_to(root).as_posix()
    key = root_key(root)
    identity = hashlib.sha256(f"{key}\0{relative.casefold()}".encode("utf-8")).hexdigest()
    fingerprint = hashlib.sha256(f"{identity}\0{size}\0{mtime_ns}".encode("utf-8")).hexdigest()
    suffix = path.suffix.casefold()
    return AssetRecord(identity, key, root, str(path), relative, path.name, suffix, _media_type(suffix), size, mtime_ns, fingerprint)


def record_file(path: str | os.PathLike, root: str | os.PathLike) -> AssetRecord | None:
    root = canonical_root(root)
    path = Path(canonical_root(path))
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if path.suffix.casefold() not in MEDIA_EXTENSIONS or path.name.startswith((".", "~$")):
        return None
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        return None
    return _record(root, path, info.st_size, info.st_mtime_ns)


def iter_scan(root, stop_event=None, on_error=None):
    root = canonical_root(root)
    if not Path(root).is_dir():
        raise FileNotFoundError(f"素材目录不可访问：{root}")

    def report(exc):
        if on_error:
            on_error(getattr(exc, "filename", root) or root, str(exc))
        else:
            raise exc

    for directory, directories, names in os.walk(root, topdown=True, followlinks=False, onerror=report):
        if stop_event is not None and stop_event.is_set():
            return
        directories[:] = sorted((name for name in directories if not name.startswith(".") and name.casefold() not in SKIP_DIRECTORY_NAMES and not (Path(directory) / name).is_symlink()), key=str.casefold)
        for name in sorted(names, key=str.casefold):
            if stop_event is not None and stop_event.is_set():
                return
            if Path(name).suffix.casefold() not in MEDIA_EXTENSIONS or name.startswith((".", "~$")) or name.endswith("~"):
                continue
            path = Path(directory) / name
            try:
                info = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or path.is_symlink():
                    continue
                yield _record(root, path, info.st_size, info.st_mtime_ns)
            except OSError as exc:
                report(exc)


def scan_files(root) -> list[AssetRecord]:
    try:
        return sorted(iter_scan(root, on_error=lambda *_: None), key=lambda item: item.relative_path.casefold())
    except (OSError, TypeError, ValueError):
        return []


def open_index(path) -> sqlite3.Connection:
    memory = str(path) == ":memory:"
    if not memory:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(":memory:" if memory else str(path), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    columns = {row[1] for row in db.execute("PRAGMA table_info(assets)")}
    legacy = []
    if columns and "root_key" not in columns:
        legacy = list(db.execute("SELECT * FROM assets"))
        db.execute("ALTER TABLE assets RENAME TO assets_v1")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS assets (
            asset_id TEXT PRIMARY KEY, root_key TEXT NOT NULL, root TEXT NOT NULL,
            path TEXT NOT NULL, relative_path TEXT NOT NULL, name TEXT NOT NULL,
            extension TEXT NOT NULL, media_type TEXT NOT NULL, size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL, file_hash TEXT NOT NULL,
            indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(root_key,relative_path COLLATE NOCASE)
        );
        CREATE INDEX IF NOT EXISTS idx_assets_library_name ON assets(root_key,name COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_assets_library_type ON assets(root_key,media_type);
    """)
    for row in legacy:
        root = Path(row["path"])
        for _ in Path(row["relative_path"]).parts:
            root = root.parent
        item = _record(canonical_root(root), Path(canonical_root(row["path"])), row["size"], row["mtime_ns"])
        upsert_records(db, [item], commit=False)
    db.commit()
    return db


def upsert_records(db, records: Iterable[AssetRecord], *, commit: bool = True) -> int:
    names = [field.name for field in fields(AssetRecord)]
    values = [tuple(getattr(item, name) for name in names) for item in records]
    before = db.total_changes
    try:
        db.executemany(f"""INSERT INTO assets ({','.join(names)}) VALUES ({','.join('?' for _ in names)})
            ON CONFLICT(asset_id) DO UPDATE SET
            path=excluded.path,root=excluded.root,relative_path=excluded.relative_path,name=excluded.name,
            extension=excluded.extension,media_type=excluded.media_type,size=excluded.size,
            mtime_ns=excluded.mtime_ns,file_hash=excluded.file_hash,indexed_at=CURRENT_TIMESTAMP
            WHERE assets.path<>excluded.path OR assets.file_hash<>excluded.file_hash
            OR assets.relative_path<>excluded.relative_path OR assets.name<>excluded.name""", values)
        changes = db.total_changes - before
        if commit:
            db.commit()
        return changes
    except Exception:
        if commit:
            db.rollback()
        raise


def _filters(query="", root=None, media_type=None):
    where, args = [], []
    if root is not None:
        where.append("root_key=?")
        args.append(root_key(root))
    if media_type:
        where.append("media_type=?")
        args.append(media_type)
    if query.strip():
        query = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(name LIKE ? ESCAPE '\\' OR relative_path LIKE ? ESCAPE '\\')")
        args.extend([f"%{query}%"] * 2)
    return (" WHERE " + " AND ".join(where) if where else ""), args


def _from_row(row):
    return AssetRecord(**{field.name: row[field.name] for field in fields(AssetRecord)})


def load_records(db, query="", *, root=None, limit=None, offset=0, media_type=None):
    where, args = _filters(query, root, media_type)
    sql = "SELECT * FROM assets" + where + " ORDER BY relative_path COLLATE NOCASE,asset_id LIMIT ? OFFSET ?"
    return [_from_row(row) for row in db.execute(sql, [*args, -1 if limit is None else max(0, limit), max(0, offset)])]


def count_records(db, query="", *, root=None, media_type=None):
    where, args = _filters(query, root, media_type)
    return db.execute("SELECT count(*) FROM assets" + where, args).fetchone()[0]


def get_record(db, asset_id):
    row = db.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    return _from_row(row) if row else None


def get_records_by_ids(db, identities):
    result = {}
    for start in range(0, len(identities), 400):
        chunk = identities[start:start + 400]
        for row in db.execute(f"SELECT * FROM assets WHERE asset_id IN ({','.join('?' for _ in chunk)})", chunk):
            record = _from_row(row)
            result[record.asset_id] = record
    return result
