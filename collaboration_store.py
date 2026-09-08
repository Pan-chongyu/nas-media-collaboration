"""Local script/ticket records replicated as immutable revisions over SMB.

Revision ancestry, rather than wall-clock timestamps, defines current versions.
Parallel heads are retained until an explicit resolution references all of them.
"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from sync_events import Event


ENTITY_TYPE = "collaboration_revision"
STATUSES = {"script": ("草稿", "待审核", "已定稿"), "work_order": ("待处理", "进行中", "待审核", "已完成", "已取消")}
DATA_FIELDS = frozenset(("title", "body", "asset_ids", "status", "assignee", "due_date", "author", "archived"))
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class CollaborationConflictError(ValueError):
    """An editor's base revision is stale or has unresolved parallel changes."""


ConflictError = CollaborationConflictError

def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identity(value, label, *, digest=False):
    if not isinstance(value, str) or not (_HEX if digest else _ID).fullmatch(value):
        raise ValueError(f"{label}格式不正确")
    return value


def _text(value, label, maximum, *, required=False, multiline=False):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{label}必须为不超过 {maximum} 字的文本")
    if any(ord(char) < 32 and not (multiline and char in "\r\n\t") for char in value):
        raise ValueError(f"{label}含有无效控制字符")
    result = value if multiline else value.strip()
    if required and not result:
        raise ValueError(f"{label}不能为空")
    return result


def _kind(value):
    if not isinstance(value, str) or value not in STATUSES:
        raise ValueError("记录类型必须为 script 或 work_order")
    return value


def _data(kind, data, base=None):
    _kind(kind)
    if not isinstance(data, dict) or set(data) - DATA_FIELDS:
        raise ValueError("协作记录包含不支持的字段")
    value = dict(title="", body="", asset_ids=[], status=STATUSES[kind][0],
                 assignee="", due_date="", author="", archived=False)
    if base:
        value.update({key: base[key] for key in DATA_FIELDS})
    value.update(data)
    value["title"] = _text(value["title"], "标题", 200, required=True)
    value["body"] = _text(value["body"], "正文", 100_000, multiline=True)
    value["assignee"] = _text(value["assignee"], "负责人", 100)
    value["author"] = _text(value["author"], "作者", 100)
    value["due_date"] = _text(value["due_date"], "截止日期", 10)
    if value["due_date"]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value["due_date"]):
            raise ValueError("截止日期应为 YYYY-MM-DD")
        try:
            date.fromisoformat(value["due_date"])
        except ValueError as exc:
            raise ValueError("截止日期不是有效日期") from exc
    if not isinstance(value["status"], str) or value["status"] not in STATUSES[kind]:
        raise ValueError("记录状态不正确")
    if type(value["archived"]) is not bool:
        raise ValueError("归档状态必须为布尔值")
    if not isinstance(value["asset_ids"], list) or len(value["asset_ids"]) > 1000:
        raise ValueError("绑定素材必须为列表，最多 1000 个")
    value["asset_ids"] = sorted({_identity(item, "素材 ID", digest=True) for item in value["asset_ids"]})
    return value


def _heads(value):
    if not isinstance(value, (list, tuple)) or len(value) > 1000:
        raise ValueError("版本列表格式不正确")
    values = [_identity(item, "版本 ID", digest=True) for item in value]
    if len(set(values)) != len(values):
        raise ValueError("版本列表不能重复")
    return sorted(values)


def _revision_hash(entity_id, device_id, payload):
    content = dict(payload)
    content.pop("revision", None)
    content.update(entity_id=entity_id, device_id=device_id)
    return hashlib.sha256(_json(content).encode("utf-8")).hexdigest()


class CollaborationStore:
    def __init__(self, db_path: Path, library_key: str, device_id: str):
        self.db_path = Path(db_path)
        self.library_key = _identity(library_key, "素材库 ID", digest=True)
        if not isinstance(device_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", device_id):
            raise ValueError("节点 ID 格式不正确")
        self.device_id = device_id
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT PRIMARY KEY, library_key TEXT NOT NULL,
                    data_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collaboration_revisions (
                    library_key TEXT NOT NULL, revision TEXT NOT NULL, entity_id TEXT NOT NULL,
                    kind TEXT NOT NULL, parents_json TEXT NOT NULL, data_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL, device_id TEXT NOT NULL, event_json TEXT NOT NULL,
                    PRIMARY KEY(library_key,revision)
                );
                CREATE INDEX IF NOT EXISTS idx_collaboration_entity
                    ON collaboration_revisions(library_key,entity_id,revision);
                CREATE TABLE IF NOT EXISTS collaboration_parents (
                    library_key TEXT NOT NULL, entity_id TEXT NOT NULL,
                    child TEXT NOT NULL, parent TEXT NOT NULL,
                    PRIMARY KEY(library_key,child,parent)
                );
                CREATE INDEX IF NOT EXISTS idx_collaboration_parent
                    ON collaboration_parents(library_key,parent);
                CREATE TABLE IF NOT EXISTS collaboration_entities (
                    library_key TEXT NOT NULL, entity_id TEXT NOT NULL, kind TEXT NOT NULL,
                    revision TEXT NOT NULL, heads_json TEXT NOT NULL, data_json TEXT NOT NULL,
                    title TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL,
                    assignee TEXT NOT NULL, author TEXT NOT NULL, archived INTEGER NOT NULL,
                    updated_at TEXT NOT NULL, conflict INTEGER NOT NULL,
                    PRIMARY KEY(library_key,entity_id)
                );
                CREATE INDEX IF NOT EXISTS idx_collaboration_list
                    ON collaboration_entities(library_key,kind,archived,updated_at DESC,entity_id);
                CREATE TABLE IF NOT EXISTS collaboration_assets (
                    library_key TEXT NOT NULL, entity_id TEXT NOT NULL, asset_id TEXT NOT NULL,
                    PRIMARY KEY(library_key,entity_id,asset_id)
                );
                CREATE INDEX IF NOT EXISTS idx_collaboration_asset
                    ON collaboration_assets(library_key,asset_id,entity_id);
            """)
            db.commit()

    def _connect(self):
        db = sqlite3.connect(str(self.db_path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def _record(row):
        if row is None:
            return None
        return dict(json.loads(row["data_json"]), entity_id=row["entity_id"], kind=row["kind"],
                    revision=row["revision"], heads=json.loads(row["heads_json"]),
                    updated_at=row["updated_at"], conflict=bool(row["conflict"]),
                    conflict_count=max(0, len(json.loads(row["heads_json"])) - 1))

    def _get(self, db, entity_id):
        return self._record(db.execute("SELECT * FROM collaboration_entities WHERE library_key=? AND entity_id=?",
                                       (self.library_key, entity_id)).fetchone())

    def get(self, entity_id):
        _identity(entity_id, "记录 ID")
        with closing(self._connect()) as db:
            return self._get(db, entity_id)

    def list_records(self, kind, query="", status="", asset_id="", include_archived=False, offset=0, limit=100):
        _kind(kind)
        query = _text(query, "搜索内容", 500)
        if not isinstance(status, str) or status and status not in STATUSES[kind]:
            raise ValueError("筛选状态不正确")
        if type(include_archived) is not bool or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("分页或归档筛选参数不正确")
        where, args = ["c.library_key=?", "c.kind=?"], [self.library_key, kind]
        if not include_archived:
            # A parallel archive must not hide a still-active version from colleagues.
            where.append("c.archived=0")
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(c.title LIKE ? ESCAPE '\\' OR c.body LIKE ? ESCAPE '\\' OR c.assignee LIKE ? ESCAPE '\\' OR c.author LIKE ? ESCAPE '\\')")
            args.extend([f"%{escaped}%"] * 4)
        if status:
            where.append("c.status=?")
            args.append(status)
        if asset_id is not None and asset_id != "":
            _identity(asset_id, "素材 ID", digest=True)
            where.append("EXISTS (SELECT 1 FROM collaboration_assets a WHERE a.library_key=c.library_key AND a.entity_id=c.entity_id AND a.asset_id=?)")
            args.append(asset_id)
        condition = " AND ".join(where)
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            total = db.execute(f"SELECT count(*) FROM collaboration_entities c WHERE {condition}", args).fetchone()[0]
            rows = db.execute(f"SELECT c.* FROM collaboration_entities c WHERE {condition} ORDER BY c.updated_at DESC,c.entity_id LIMIT ? OFFSET ?",
                              [*args, limit, offset]).fetchall()
            return [self._record(row) for row in rows], total

    def versions(self, entity_id):
        """Return all retained history, with current parallel heads first."""
        _identity(entity_id, "记录 ID")
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            record = self._get(db, entity_id)
            if record is None:
                return []
            heads = record["heads"]
            rows = db.execute("SELECT * FROM collaboration_revisions WHERE library_key=? AND entity_id=? ORDER BY revision",
                              (self.library_key, entity_id)).fetchall()
            result = [dict(json.loads(row["data_json"]), entity_id=entity_id, kind=row["kind"], revision=row["revision"],
                           heads=list(heads), updated_at=row["updated_at"], conflict=len(heads) > 1, conflict_count=max(0, len(heads) - 1),
                           parents=json.loads(row["parents_json"]), device_id=row["device_id"], is_head=row["revision"] in heads, is_current=row["revision"] in heads)
                      for row in rows]
            return sorted(result, key=lambda item: (not item["is_head"], item["revision"]))

    def _project(self, db, entity_id):
        rows = db.execute("""SELECT r.* FROM collaboration_revisions r WHERE r.library_key=? AND r.entity_id=?
            AND NOT EXISTS (SELECT 1 FROM collaboration_parents p WHERE p.library_key=r.library_key AND p.parent=r.revision)
            ORDER BY r.revision""", (self.library_key, entity_id)).fetchall()
        if not rows:
            raise ValueError("协作版本关系无有效终点")
        heads = [row["revision"] for row in rows]
        # Stable representative only; no branch is discarded or silently resolved.
        row = rows[0]
        data = json.loads(row["data_json"])
        versions = [json.loads(item["data_json"]) for item in rows]
        archived = all(item["archived"] for item in versions)
        db.execute("""INSERT INTO collaboration_entities VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(library_key,entity_id) DO UPDATE SET revision=excluded.revision,
            heads_json=excluded.heads_json,data_json=excluded.data_json,title=excluded.title,body=excluded.body,
            status=excluded.status,assignee=excluded.assignee,author=excluded.author,archived=excluded.archived,
            updated_at=excluded.updated_at,conflict=excluded.conflict""",
            (self.library_key, entity_id, row["kind"], row["revision"], _json(heads), row["data_json"], data["title"],
             data["body"], data["status"], data["assignee"], data["author"], int(archived), row["updated_at"], int(len(heads) > 1)))
        db.execute("DELETE FROM collaboration_assets WHERE library_key=? AND entity_id=?", (self.library_key, entity_id))
        assets = sorted({asset for version in versions for asset in version["asset_ids"]})
        db.executemany("INSERT INTO collaboration_assets VALUES(?,?,?)", ((self.library_key, entity_id, asset) for asset in assets))

    def _validate_event(self, event):
        if event.entity_type != ENTITY_TYPE or event.operation != "append":
            raise ValueError("不支持的协作事件")
        payload = event.payload
        if not isinstance(payload, dict) or set(payload) != {"schema", "library_key", "kind", "data", "parents", "revision", "updated_at"} or type(payload["schema"]) is not int or payload["schema"] != 1:
            raise ValueError("不支持的协作事件格式")
        if payload["library_key"] != self.library_key:
            raise ValueError("协作事件属于其他素材库")
        _identity(event.entity_id, "记录 ID")
        if not isinstance(event.device_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", event.device_id):
            raise ValueError("协作事件节点 ID 不正确")
        _identity(payload["revision"], "版本 ID", digest=True)
        if event.event_id != payload["revision"] or event.timestamp != payload["updated_at"] or event.sequence is not None:
            raise ValueError("协作事件标识不匹配")
        updated = _text(payload["updated_at"], "更新时间", 40)
        try:
            if datetime.fromisoformat(updated.replace("Z", "+00:00")).tzinfo is None:
                raise ValueError("missing timezone")
        except ValueError as exc:
            raise ValueError("协作事件时间格式不正确") from exc
        data = _data(payload["kind"], payload["data"])
        parents = _heads(payload["parents"])
        if data != payload["data"] or parents != payload["parents"]:
            raise ValueError("协作事件内容未规范化")
        if payload["revision"] in parents or _revision_hash(event.entity_id, event.device_id, payload) != payload["revision"]:
            raise ValueError("协作版本内容校验失败")
        return payload

    def _apply(self, db, event):
        payload = self._validate_event(event)
        encoded = _json(event.to_dict())
        existing = db.execute("SELECT event_json FROM collaboration_revisions WHERE library_key=? AND revision=?",
                              (self.library_key, payload["revision"])).fetchone()
        if existing:
            if existing[0] != encoded:
                raise ValueError("相同版本 ID 的内容冲突")
            return False
        current = self._get(db, event.entity_id)
        if current and current["kind"] != payload["kind"]:
            raise ValueError("不能更改协作记录类型")
        for parent in payload["parents"]:
            owner = db.execute("SELECT entity_id,kind FROM collaboration_revisions WHERE library_key=? AND revision=?",
                               (self.library_key, parent)).fetchone()
            if owner and (owner["entity_id"] != event.entity_id or owner["kind"] != payload["kind"]):
                raise ValueError("父版本属于其他记录")
        children = db.execute("""SELECT r.entity_id,r.kind FROM collaboration_parents p
            JOIN collaboration_revisions r ON r.library_key=p.library_key AND r.revision=p.child
            WHERE p.library_key=? AND p.parent=?""", (self.library_key, payload["revision"])).fetchall()
        if any(row["entity_id"] != event.entity_id or row["kind"] != payload["kind"] for row in children):
            raise ValueError("子版本属于其他记录")
        db.execute("INSERT INTO collaboration_revisions VALUES(?,?,?,?,?,?,?,?,?)",
                   (self.library_key, payload["revision"], event.entity_id, payload["kind"], _json(payload["parents"]),
                    _json(payload["data"]), payload["updated_at"], event.device_id, encoded))
        db.executemany("INSERT INTO collaboration_parents VALUES(?,?,?,?)",
                       ((self.library_key, event.entity_id, payload["revision"], parent) for parent in payload["parents"]))
        self._project(db, event.entity_id)
        return True

    def apply_event(self, event: Event, db=None) -> bool:
        """Apply idempotently; a supplied connection must be in the inbox transaction."""
        if db is not None:
            if not db.in_transaction:
                raise RuntimeError("接收协作事件必须在事务中运行")
            return self._apply(db, event)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._apply(connection, event)

    def save(self, kind, data: dict, entity_id=None, expected_heads=None, resolve=False, db=None):
        """Write atomically; optional db joins an existing caller-owned transaction."""
        if db is not None:
            if not db.in_transaction:
                raise RuntimeError("写入必须在调用方事务中运行")
            return self._save(db, kind, data, entity_id, expected_heads, resolve)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._save(connection, kind, data, entity_id, expected_heads, resolve)

    def _save(self, db, kind, data: dict, entity_id=None, expected_heads=None, resolve=False):
        _kind(kind)
        if type(resolve) is not bool:
            raise ValueError("版本合并参数不正确")
        entity_id = _identity(entity_id, "记录 ID") if entity_id is not None else uuid.uuid4().hex
        expected = _heads(expected_heads) if expected_heads is not None else None
        current = self._get(db, entity_id)
        heads = current["heads"] if current else []
        if current and current["kind"] != kind:
            raise ValueError("不能更改协作记录类型")
        if current and expected is None:
            raise ConflictError("请重新打开记录后保存，以检查其他人的修改")
        if expected is not None and expected != heads:
            raise ConflictError("记录已被修改，请保留当前内容并重新载入最新版本")
        if len(heads) > 1 and not resolve:
            raise ConflictError("存在并行版本，请先查看各版本并合并保存")
        if len(heads) > 1 and (not isinstance(data, dict) or set(data) != DATA_FIELDS):
            raise ValueError("合并并行版本时必须提供完整内容")
        payload = dict(schema=1, library_key=self.library_key, kind=kind, data=_data(kind, data, current),
                       parents=heads, updated_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"))
        payload["revision"] = _revision_hash(entity_id, self.device_id, payload)
        event = Event.new(self.device_id, ENTITY_TYPE, entity_id, "append", payload,
                          event_id=payload["revision"], timestamp=payload["updated_at"])
        self._apply(db, event)
        db.execute("INSERT INTO outbox VALUES(?,?,?,?)",
                   (event.event_id, self.library_key, _json(event.to_dict()), payload["updated_at"]))
        return self._get(db, entity_id)
