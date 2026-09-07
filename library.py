"""Per-client index, recoverable scan jobs, and SMB metadata synchronization."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import threading
import time
import uuid

from indexer import (
    AssetRecord, count_records, get_record, get_records_by_ids, iter_scan,
    load_records, open_index, record_file, root_key, upsert_records,
)
from media import generate_thumbnail
from sync_events import Event, append_event, read_events


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LibraryService:
    def __init__(self, data_dir: Path, root: str, sync_root: str, device_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", device_id):
            raise ValueError("节点 ID 只能包含英文字母、数字、连字符和下划线")
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "index.sqlite3"
        self.cache_dir = self.data_dir / "thumbnails"
        self.root = root
        self.sync_root = sync_root
        self.device_id = device_id
        self._scan_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._thumbnail_slots = threading.BoundedSemaphore(2)
        with closing(open_index(self.db_path)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT PRIMARY KEY, library_key TEXT NOT NULL,
                    data_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inbox (
                    library_key TEXT NOT NULL, event_id TEXT NOT NULL,
                    received_at TEXT NOT NULL, PRIMARY KEY(library_key, event_id)
                );
                CREATE TABLE IF NOT EXISTS thumbnails (
                    asset_id TEXT PRIMARY KEY, file_hash TEXT NOT NULL,
                    cache_path TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0, changed INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS rejected_events (
                    event_id TEXT PRIMARY KEY, reason TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deferred_events (
                    library_key TEXT NOT NULL, event_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL, next_retry REAL NOT NULL, error TEXT NOT NULL,
                    PRIMARY KEY(library_key, event_id)
                );
            """)
            db.commit()

    def _state(self, db, key: str, value: str) -> None:
        db.execute("INSERT INTO app_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def overview(self) -> dict:
        with closing(open_index(self.db_path)) as db:
            state = {row[0]: row[1] for row in db.execute("SELECT key,value FROM app_state")}
            state.update(
                total=count_records(db, root=self.root),
                pending=db.execute("SELECT count(*) FROM outbox WHERE library_key=?", (root_key(self.root),)).fetchone()[0],
                jobs=[dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 40")],
                rejected=db.execute("SELECT count(*) FROM rejected_events").fetchone()[0],
                deferred=db.execute("SELECT count(*) FROM deferred_events WHERE library_key=?", (root_key(self.root),)).fetchone()[0],
            )
            return state

    def page(self, query: str = "", media_type: str | None = None, offset: int = 0, limit: int = 30) -> tuple[list[dict], int]:
        with closing(open_index(self.db_path)) as db:
            count = count_records(db, query, root=self.root, media_type=media_type)
            records = load_records(db, query, root=self.root, media_type=media_type, offset=offset, limit=limit)
            items = []
            for record in records:
                item = asdict(record)
                thumb = db.execute("SELECT cache_path,status,file_hash FROM thumbnails WHERE asset_id=?", (record.asset_id,)).fetchone()
                item["thumbnail"] = thumb["cache_path"] if thumb and thumb["file_hash"] == record.file_hash else ""
                item["thumbnail_status"] = thumb["status"] if thumb and thumb["file_hash"] == record.file_hash else "pending"
                items.append(item)
            return items, count

    def _job(self, job_id: str, kind: str, status: str, done: int, changed: int, errors: int, message: str) -> dict:
        result = dict(job_id=job_id, kind=kind, status=status, done=done, changed=changed, errors=errors, message=message, updated_at=utc_now())
        with closing(open_index(self.db_path)) as db, db:
            db.execute("""INSERT INTO jobs VALUES(:job_id,:kind,:status,:done,:changed,:errors,:message,:updated_at)
                       ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,done=excluded.done,
                       changed=excluded.changed,errors=excluded.errors,message=excluded.message,updated_at=excluded.updated_at""", result)
        return result

    def _commit_batch(self, db, records: list[AssetRecord], publish: bool = True) -> int:
        existing = get_records_by_ids(db, [item.asset_id for item in records])
        # The outbox and index commit together, including when the NAS is offline.
        changed = [item for item in records if item.asset_id not in existing or asdict(item) != asdict(existing[item.asset_id])]
        if not changed:
            return 0
        with db:
            upsert_records(db, changed, commit=False)
            if publish:
                payloads = []
                for item in changed:
                    payload = asdict(item)
                    payload.pop("path", None)
                    payload.pop("root", None)
                    payload.pop("root_path", None)
                    payloads.append(payload)
                event = Event.new(self.device_id, "asset_batch", uuid.uuid4().hex, "upsert", {
                    "schema": 1, "library_key": root_key(self.root), "records": payloads,
                })
                db.execute("INSERT INTO outbox VALUES(?,?,?,?)", (event.event_id, root_key(self.root), json.dumps(event.to_dict(), ensure_ascii=False), utc_now()))
        return len(changed)

    def scan(self, stop: threading.Event | None = None, progress=None) -> dict:
        if not self._scan_lock.acquire(blocking=False):
            raise RuntimeError("扫描已在运行")
        stop = stop or threading.Event()
        job_id = uuid.uuid4().hex
        done = changed = 0
        errors: list[str] = []
        error_count = 0

        def on_error(*args):
            nonlocal error_count
            error_count += 1
            if len(errors) < 3:
                errors.append(" ".join(map(str, args)))

        def report(status: str, message: str):
            value = self._job(job_id, "scan", status, done, changed, error_count, message)
            if progress:
                progress(value)
            return value

        try:
            report("running", "正在枚举素材目录")
            with closing(open_index(self.db_path)) as db:
                batch = []
                for record in iter_scan(self.root, stop_event=stop, on_error=on_error):
                    batch.append(record)
                    done += 1
                    if len(batch) >= 50:
                        changed += self._commit_batch(db, batch)
                        batch.clear()
                        report("running", record.relative_path)
                if batch:
                    changed += self._commit_batch(db, batch)
                status = "paused" if stop.is_set() else ("partial" if error_count else "done")
                message = "; ".join(errors) if error_count else ("已保留扫描进度" if stop.is_set() else "扫描完成")
                with db:
                    self._state(db, "last_scan", utc_now())
                    self._state(db, "scan_status", status)
            return report(status, message)
        except Exception as exc:
            error_count += 1
            return report("failed", str(exc))
        finally:
            self._scan_lock.release()

    def import_files(self, paths: list[str], progress=None) -> dict:
        if not self._scan_lock.acquire(blocking=False):
            raise RuntimeError("请等待扫描完成或暂停扫描")
        job_id = uuid.uuid4().hex
        changed = skipped = 0
        try:
            with closing(open_index(self.db_path)) as db:
                records = []
                for path in paths:
                    try:
                        record = record_file(path, self.root)
                    except (OSError, ValueError):
                        record = None
                    if record:
                        records.append(record)
                    else:
                        skipped += 1
                for start in range(0, len(records), 50):
                    changed += self._commit_batch(db, records[start:start + 50])
            result = self._job(job_id, "import", "partial" if skipped else "done", len(paths), changed, skipped, "仅索引素材根目录内的文件")
            if progress:
                progress(result)
            return result
        finally:
            self._scan_lock.release()

    def _incoming_record(self, payload: dict) -> AssetRecord:
        if not isinstance(payload, dict):
            raise ValueError("素材记录格式不正确")
        relative = payload.get("relative_path", "")
        if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative or "\x00" in relative:
            raise ValueError("无效的素材相对路径")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or PureWindowsPath(relative).is_absolute() or ".." in pure.parts:
            raise ValueError("素材路径超出根目录")
        path = str(Path(self.root).joinpath(*pure.parts))
        record = record_file(path, self.root)
        if record is None:
            raise FileNotFoundError(path)
        if record.asset_id != payload.get("asset_id"):
            raise ValueError("素材 ID 与路径不匹配")
        # A fresh stat prevents a late event from overwriting a newer observation.
        return record

    def _resolve_records(self, payloads: list[dict]) -> tuple[list[AssetRecord], list[dict], str]:
        records, pending, message = [], [], ""
        for payload in payloads:
            try:
                records.append(self._incoming_record(payload))
            except OSError as exc:
                pending.append(payload)
                message = str(exc)
        return records, pending, message

    def _defer(self, db, key, event_id, payloads, error, attempts=1):
        if payloads:
            db.execute("""INSERT INTO deferred_events VALUES(?,?,?,?,?,?)
                ON CONFLICT(library_key,event_id) DO UPDATE SET payload_json=excluded.payload_json,
                attempts=excluded.attempts,next_retry=excluded.next_retry,error=excluded.error""",
                (key, event_id, json.dumps(payloads, ensure_ascii=False), attempts,
                 time.time() + min(1800, 30 * 2 ** min(attempts - 1, 6)), error))
        else:
            db.execute("DELETE FROM deferred_events WHERE library_key=? AND event_id=?", (key, event_id))

    def sync_once(self, stop: threading.Event | None = None) -> dict:
        if not self._sync_lock.acquire(blocking=False):
            return {"status": "busy", "sent": 0, "received": 0, "changed": 0}
        stop = stop or threading.Event()
        key = root_key(self.root)
        sent = received = changed = rejected = 0
        try:
            shared = Path(self.sync_root)
            if not self.sync_root.strip():
                raise OSError("尚未配置协作数据目录")
            if not shared.parent.is_dir():
                raise OSError("协作数据目录的上级共享不可用")
            shared.mkdir(exist_ok=True)
            with closing(open_index(self.db_path)) as db:
                pending = db.execute("SELECT * FROM outbox WHERE library_key=? ORDER BY created_at,event_id LIMIT 128", (key,)).fetchall()
                for row in pending:
                    if stop.is_set():
                        break
                    event = Event.from_dict(json.loads(row["data_json"]))
                    if append_event(shared, event, strict=True) is None:
                        raise OSError("事件尚未写入 NAS")
                    with db:
                        db.execute("DELETE FROM outbox WHERE event_id=?", (event.event_id,))
                        db.execute("INSERT OR IGNORE INTO inbox VALUES(?,?,?)", (key, event.event_id, utc_now()))
                    sent += 1
                retries = db.execute("""SELECT * FROM deferred_events WHERE library_key=?
                    AND next_retry<=? ORDER BY next_retry LIMIT 16""", (key, time.time())).fetchall()
                for row in retries:
                    if stop.is_set():
                        break
                    records, remaining, error = self._resolve_records(json.loads(row["payload_json"]))
                    with db:
                        changed += upsert_records(db, records, commit=False)
                        self._defer(db, key, row["event_id"], remaining, error, row["attempts"] + 1)
                seen = {row[0] for row in db.execute("SELECT event_id FROM inbox WHERE library_key=?", (key,))}
                events = read_events(shared, seen_ids=seen, limit=256, strict=True)
                for event in events:
                    if stop.is_set():
                        break
                    records = []
                    remaining, retry_error = [], ""
                    try:
                        if event.entity_type == "asset_batch" and event.operation == "upsert" and event.payload.get("library_key") == key:
                            payloads = event.payload.get("records")
                            if event.payload.get("schema") != 1 or not isinstance(payloads, list) or len(payloads) > 100:
                                raise ValueError("不支持的素材事件格式")
                            records, remaining, retry_error = self._resolve_records(payloads)
                    except (ValueError, TypeError) as exc:
                        with db:
                            db.execute("INSERT OR IGNORE INTO rejected_events VALUES(?,?,?)", (event.event_id, str(exc), utc_now()))
                            db.execute("INSERT OR IGNORE INTO inbox VALUES(?,?,?)", (key, event.event_id, utc_now()))
                        rejected += 1
                        continue
                    with db:
                        changed += upsert_records(db, records, commit=False)
                        self._defer(db, key, event.event_id, remaining, retry_error)
                        db.execute("INSERT OR IGNORE INTO inbox VALUES(?,?,?)", (key, event.event_id, utc_now()))
                    received += 1
                deferred = db.execute("SELECT count(*) FROM deferred_events WHERE library_key=?", (key,)).fetchone()[0]
                with db:
                    self._state(db, "last_sync", utc_now())
                    self._state(db, "sync_error", "")
            return {"status": "done", "sent": sent, "received": received, "changed": changed, "rejected": rejected, "deferred": deferred}
        except Exception as exc:
            with closing(open_index(self.db_path)) as db, db:
                self._state(db, "sync_error", str(exc))
            return {"status": "offline", "sent": sent, "received": received, "changed": changed, "error": str(exc)}
        finally:
            self._sync_lock.release()

    def thumbnail(self, asset_id: str, fingerprint: str) -> dict:
        with self._thumbnail_slots, closing(open_index(self.db_path)) as db:
            record = get_record(db, asset_id)
            if record is None or record.file_hash != fingerprint or record.media_type not in {"video", "image"}:
                return {"asset_id": asset_id, "file_hash": fingerprint, "path": "", "status": "skipped"}
            try:
                path = generate_thumbnail(record.path, self.cache_dir)
            except (OSError, ValueError, Image.DecompressionBombError):
                path = None
            status = "ready" if path else "failed"
            with db:
                current = get_record(db, asset_id)
                if current and current.file_hash == fingerprint:
                    db.execute("""INSERT INTO thumbnails VALUES(?,?,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET
                               file_hash=excluded.file_hash,cache_path=excluded.cache_path,status=excluded.status,updated_at=excluded.updated_at""",
                               (asset_id, fingerprint, str(path or ""), status, utc_now()))
            return {"asset_id": asset_id, "file_hash": fingerprint, "path": str(path or ""), "status": status}

    def batch_thumbnails(self, stop: threading.Event | None = None, progress=None) -> dict:
        stop = stop or threading.Event()
        job_id = uuid.uuid4().hex
        done = success = errors = 0
        last_id = ""
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="thumbnail") as pool:
            while not stop.is_set():
                with closing(open_index(self.db_path)) as db:
                    rows = db.execute("""SELECT a.asset_id,a.file_hash FROM assets a LEFT JOIN thumbnails t ON a.asset_id=t.asset_id
                        WHERE a.root_key=? AND a.media_type IN ('video','image') AND a.asset_id>?
                        AND (t.asset_id IS NULL OR t.file_hash<>a.file_hash OR t.status<>'ready')
                        ORDER BY a.asset_id LIMIT 8""", (root_key(self.root), last_id)).fetchall()
                if not rows:
                    break
                last_id = rows[-1]["asset_id"]
                futures = [pool.submit(self.thumbnail, row["asset_id"], row["file_hash"]) for row in rows]
                for future in futures:
                    try:
                        result = future.result()
                        success += result["status"] == "ready"
                        errors += result["status"] == "failed"
                    except Exception:
                        errors += 1
                    done += 1
                    report = self._job(job_id, "thumbnails", "running", done, success, errors, "正在生成缩略图")
                    if progress:
                        progress(report)
        return self._job(job_id, "thumbnails", "paused" if stop.is_set() else "done", done, success, errors, "已暂停" if stop.is_set() else "缩略图批处理完成")
