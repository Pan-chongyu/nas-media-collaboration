"""Local categories and observed-remove memberships, replicated over immutable events.

Category revisions preserve concurrent edits; membership removals only remove add
tags that the editing client has observed. None of these operations touch media.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
import uuid

from sync_events import Event
from indexer import open_index


ENTITY_TYPE = "category_revision"
MEMBERSHIP_ENTITY_TYPE = "category_membership"
ENTITY_TYPES = frozenset((ENTITY_TYPE, MEMBERSHIP_ENTITY_TYPE))
DATA_FIELDS = frozenset(("name", "color", "archived"))
UNCATEGORIZED = "__uncategorized__"
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class CategoryConflictError(ValueError):
    """A category editor has stale or unresolved parallel revisions."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identity(value, *, digest=False):
    if not isinstance(value, str) or not (_HEX if digest else _ID).fullmatch(value) or value == UNCATEGORIZED:
        raise ValueError("分类、素材或版本 ID 格式不正确")
    return value


def _ids(values, *, digest=False, maximum=1000, allow_empty=True):
    if not isinstance(values, (list, tuple)) or len(values) > maximum or not values and not allow_empty:
        raise ValueError("ID 列表格式或数量不正确")
    return sorted({_identity(item, digest=digest) for item in values})


def _heads(values):
    result = _ids(values, digest=True)
    if len(result) != len(values):
        raise ValueError("版本列表不能重复")
    return result


def _name_key(value):
    return unicodedata.normalize("NFKC", value).casefold()


def _data(data, base=None):
    if not isinstance(data, dict) or set(data) - DATA_FIELDS:
        raise ValueError("分类包含不支持的字段")
    value = dict(name="", color="#0d9488", archived=False)
    if base:
        value.update({key: base[key] for key in DATA_FIELDS})
    value.update(data)
    name = value["name"]
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
        raise ValueError("分类名称必须为 1 至 40 个字")
    if any(unicodedata.category(char).startswith("C") for char in name):
        raise ValueError("分类名称不能包含控制字符")
    value["name"] = name.strip()
    if not isinstance(value["color"], str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value["color"]):
        raise ValueError("分类颜色应为 #rrggbb")
    value["color"] = value["color"].lower()
    if type(value["archived"]) is not bool:
        raise ValueError("归档状态必须为布尔值")
    return value


def _event_hash(entity_type, entity_id, device_id, payload):
    content = dict(payload)
    content.pop("revision", None)
    content.update(entity_type=entity_type, entity_id=entity_id, device_id=device_id)
    return hashlib.sha256(_json(content).encode("utf-8")).hexdigest()


class CategoryStore:
    def __init__(self, db_path: Path, library_key: str, device_id: str):
        self.db_path = Path(db_path)
        self.library_key = _identity(library_key, digest=True)
        if not isinstance(device_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", device_id):
            raise ValueError("节点 ID 格式不正确")
        self.device_id = device_id
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # The store is also usable in isolated recovery tools before LibraryService.
        with closing(open_index(self.db_path)):
            pass
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT PRIMARY KEY, library_key TEXT NOT NULL,
                    data_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS category_revisions (
                    library_key TEXT NOT NULL, revision TEXT NOT NULL, category_id TEXT NOT NULL,
                    parents_json TEXT NOT NULL, data_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                    device_id TEXT NOT NULL, event_json TEXT NOT NULL,
                    PRIMARY KEY(library_key,revision)
                );
                CREATE INDEX IF NOT EXISTS idx_category_revision_entity
                    ON category_revisions(library_key,category_id,revision);
                CREATE TABLE IF NOT EXISTS category_parents (
                    library_key TEXT NOT NULL, category_id TEXT NOT NULL,
                    child TEXT NOT NULL, parent TEXT NOT NULL,
                    PRIMARY KEY(library_key,child,parent)
                );
                CREATE INDEX IF NOT EXISTS idx_category_parent ON category_parents(library_key,parent);
                CREATE TABLE IF NOT EXISTS category_entities (
                    library_key TEXT NOT NULL, category_id TEXT NOT NULL, revision TEXT NOT NULL,
                    heads_json TEXT NOT NULL, data_json TEXT NOT NULL, name TEXT NOT NULL,
                    name_key TEXT NOT NULL, archived INTEGER NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(library_key,category_id)
                );
                CREATE INDEX IF NOT EXISTS idx_category_list ON category_entities(library_key,archived,name_key);
                CREATE TABLE IF NOT EXISTS category_membership_events (
                    library_key TEXT NOT NULL, event_id TEXT NOT NULL, event_json TEXT NOT NULL,
                    PRIMARY KEY(library_key,event_id)
                );
                CREATE TABLE IF NOT EXISTS category_member_adds (
                    library_key TEXT NOT NULL, category_id TEXT NOT NULL, asset_id TEXT NOT NULL,
                    tag TEXT NOT NULL, PRIMARY KEY(library_key,category_id,asset_id,tag)
                );
                CREATE TABLE IF NOT EXISTS category_member_removes (
                    library_key TEXT NOT NULL, category_id TEXT NOT NULL, asset_id TEXT NOT NULL,
                    tag TEXT NOT NULL, PRIMARY KEY(library_key,category_id,asset_id,tag)
                );
                CREATE TABLE IF NOT EXISTS category_memberships (
                    library_key TEXT NOT NULL, category_id TEXT NOT NULL, asset_id TEXT NOT NULL,
                    PRIMARY KEY(library_key,category_id,asset_id)
                );
                CREATE INDEX IF NOT EXISTS idx_category_membership_asset
                    ON category_memberships(library_key,asset_id,category_id);
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
        heads = json.loads(row["heads_json"])
        return dict(json.loads(row["data_json"]), category_id=row["category_id"], revision=row["revision"],
                    heads=heads, archived=bool(row["archived"]), conflict_count=max(0, len(heads) - 1),
                    updated_at=row["updated_at"], count=row["count"] if "count" in row.keys() else 0)

    _COUNTS = """SELECT c.*, (SELECT count(*) FROM category_memberships m JOIN assets a
        ON a.root_key=m.library_key AND a.asset_id=m.asset_id
        WHERE m.library_key=c.library_key AND m.category_id=c.category_id) AS count
        FROM category_entities c"""

    def _get(self, db, category_id):
        return self._record(db.execute(self._COUNTS + " WHERE c.library_key=? AND c.category_id=?",
                                      (self.library_key, category_id)).fetchone())

    def get(self, category_id):
        _identity(category_id)
        with closing(self._connect()) as db:
            return self._get(db, category_id)

    def list_records(self, include_archived=False):
        if type(include_archived) is not bool:
            raise ValueError("归档筛选参数不正确")
        with closing(self._connect()) as db:
            return [self._record(row) for row in db.execute(self._COUNTS +
                " WHERE c.library_key=?" + ("" if include_archived else " AND c.archived=0") +
                " ORDER BY c.name_key,c.category_id", (self.library_key,))]

    def for_assets(self, asset_ids, db=None):
        identities = _ids(asset_ids, digest=True, maximum=10000)
        if db is None:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                return self.for_assets(identities, connection)
        result = {identity: [] for identity in identities}
        for start in range(0, len(identities), 400):
            chunk = identities[start:start + 400]
            rows = db.execute("""SELECT c.*,m.asset_id FROM category_memberships m JOIN category_entities c
                ON c.library_key=m.library_key AND c.category_id=m.category_id
                WHERE c.library_key=? AND c.archived=0 AND m.asset_id IN (""" + ",".join("?" for _ in chunk) +
                ") ORDER BY c.name_key,c.category_id", [self.library_key, *chunk])
            for row in rows:
                result[row["asset_id"]].append(self._record(row))
        # Count each distinct category once for this page, rather than issuing
        # a count query for every material badge.
        category_ids = sorted({record["category_id"] for records in result.values() for record in records})
        counts = {}
        for start in range(0, len(category_ids), 400):
            chunk = category_ids[start:start + 400]
            rows = db.execute("""SELECT m.category_id,count(*) AS count FROM category_memberships m
                JOIN assets a ON a.root_key=m.library_key AND a.asset_id=m.asset_id
                WHERE m.library_key=? AND m.category_id IN (""" + ",".join("?" for _ in chunk) +
                ") GROUP BY m.category_id", [self.library_key, *chunk])
            counts.update({row["category_id"]: row["count"] for row in rows})
        for records in result.values():
            for record in records:
                record["count"] = counts.get(record["category_id"], 0)
        return result

    def versions(self, category_id):
        _identity(category_id)
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            current = self._get(db, category_id)
            if current is None:
                return []
            heads = current["heads"]
            result = [dict(json.loads(row["data_json"]), category_id=category_id, revision=row["revision"],
                heads=list(heads), updated_at=row["updated_at"], conflict_count=max(0, len(heads) - 1),
                count=current["count"], parents=json.loads(row["parents_json"]), device_id=row["device_id"],
                is_head=row["revision"] in heads)
                for row in db.execute("SELECT * FROM category_revisions WHERE library_key=? AND category_id=? ORDER BY revision",
                                      (self.library_key, category_id))]
            return sorted(result, key=lambda item: (not item["is_head"], item["revision"]))

    def _project_category(self, db, category_id):
        rows = db.execute("""SELECT r.* FROM category_revisions r WHERE r.library_key=? AND r.category_id=?
            AND NOT EXISTS (SELECT 1 FROM category_parents p WHERE p.library_key=r.library_key AND p.parent=r.revision)
            ORDER BY r.revision""", (self.library_key, category_id)).fetchall()
        if not rows:
            raise ValueError("分类版本关系无有效终点")
        heads = [row["revision"] for row in rows]
        # Conflicting archives cannot hide an active branch; all versions remain accessible.
        active = [row for row in rows if not json.loads(row["data_json"])["archived"]]
        row = (active or rows)[0]
        data = json.loads(row["data_json"])
        db.execute("""INSERT INTO category_entities VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(library_key,category_id) DO UPDATE SET revision=excluded.revision,
            heads_json=excluded.heads_json,data_json=excluded.data_json,name=excluded.name,
            name_key=excluded.name_key,archived=excluded.archived,updated_at=excluded.updated_at""",
            (self.library_key, category_id, row["revision"], _json(heads), row["data_json"], data["name"],
             _name_key(data["name"]), int(not active), row["updated_at"]))

    def _validate_event(self, event):
        if event.entity_type not in ENTITY_TYPES or event.operation != "append":
            raise ValueError("不支持的分类事件")
        payload = event.payload
        fields = {"schema", "library_key", "data", "parents", "revision", "updated_at"} if event.entity_type == ENTITY_TYPE else {
            "schema", "library_key", "changes", "revision", "updated_at"}
        if not isinstance(payload, dict) or set(payload) != fields or type(payload["schema"]) is not int or payload["schema"] != 1:
            raise ValueError("不支持的分类事件格式")
        if payload["library_key"] != self.library_key:
            raise ValueError("分类事件属于其他素材库")
        _identity(event.entity_id)
        _identity(payload["revision"], digest=True)
        if not isinstance(event.device_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", event.device_id):
            raise ValueError("分类事件节点 ID 不正确")
        if event.event_id != payload["revision"] or event.timestamp != payload["updated_at"] or event.sequence is not None:
            raise ValueError("分类事件标识不匹配")
        try:
            updated = payload["updated_at"]
            if not isinstance(updated, str) or len(updated) > 40 or datetime.fromisoformat(updated.replace("Z", "+00:00")).tzinfo is None:
                raise ValueError("missing timezone")
        except (ValueError, TypeError) as exc:
            raise ValueError("分类事件时间格式不正确") from exc
        if _event_hash(event.entity_type, event.entity_id, event.device_id, payload) != event.event_id:
            raise ValueError("分类事件内容校验失败")
        if event.entity_type == ENTITY_TYPE:
            if _data(payload["data"]) != payload["data"] or _heads(payload["parents"]) != payload["parents"] or event.event_id in payload["parents"]:
                raise ValueError("分类事件内容未规范化")
        else:
            changes = payload["changes"]
            if not isinstance(changes, list) or not 1 <= len(changes) <= 1000:
                raise ValueError("分类关联事件数量不正确")
            seen = set()
            for change in changes:
                if not isinstance(change, dict) or set(change) != {"category_id", "asset_id", "operation", "tags"}:
                    raise ValueError("分类关联事件格式不正确")
                pair = (_identity(change["category_id"]), _identity(change["asset_id"], digest=True))
                if pair in seen or change["operation"] not in ("add", "remove") or _heads(change["tags"]) != change["tags"]:
                    raise ValueError("分类关联事件未规范化")
                seen.add(pair)
                if change["operation"] == "add" and change["tags"] or change["operation"] == "remove" and not change["tags"]:
                    raise ValueError("分类移除必须引用已观察的添加版本")
                if event.event_id in change["tags"]:
                    raise ValueError("分类关联事件不能引用自身")
        return payload

    def _apply_category(self, db, event, payload):
        existing = db.execute("SELECT event_json FROM category_revisions WHERE library_key=? AND revision=?",
                              (self.library_key, event.event_id)).fetchone()
        encoded = _json(event.to_dict())
        if existing:
            if existing[0] != encoded:
                raise ValueError("相同分类版本 ID 的内容冲突")
            return False
        for parent in payload["parents"]:
            owner = db.execute("SELECT category_id FROM category_revisions WHERE library_key=? AND revision=?",
                               (self.library_key, parent)).fetchone()
            if owner and owner[0] != event.entity_id:
                raise ValueError("父版本属于其他分类")
        if db.execute("SELECT 1 FROM category_parents WHERE library_key=? AND parent=? AND category_id<>? LIMIT 1",
                      (self.library_key, event.event_id, event.entity_id)).fetchone():
            raise ValueError("子版本属于其他分类")
        db.execute("INSERT INTO category_revisions VALUES(?,?,?,?,?,?,?,?)", (self.library_key, event.event_id,
            event.entity_id, _json(payload["parents"]), _json(payload["data"]), payload["updated_at"], event.device_id, encoded))
        db.executemany("INSERT INTO category_parents VALUES(?,?,?,?)", ((self.library_key, event.entity_id,
            event.event_id, parent) for parent in payload["parents"]))
        self._project_category(db, event.entity_id)
        return True

    def _live_tags(self, db, category_id, asset_id):
        return [row[0] for row in db.execute("""SELECT a.tag FROM category_member_adds a
            WHERE a.library_key=? AND a.category_id=? AND a.asset_id=? AND NOT EXISTS (
                SELECT 1 FROM category_member_removes r WHERE r.library_key=a.library_key AND
                r.category_id=a.category_id AND r.asset_id=a.asset_id AND r.tag=a.tag) ORDER BY a.tag""",
            (self.library_key, category_id, asset_id))]

    def _apply_membership(self, db, event, payload):
        existing = db.execute("SELECT event_json FROM category_membership_events WHERE library_key=? AND event_id=?",
                              (self.library_key, event.event_id)).fetchone()
        encoded = _json(event.to_dict())
        if existing:
            if existing[0] != encoded:
                raise ValueError("相同分类关联 ID 的内容冲突")
            return False
        db.execute("INSERT INTO category_membership_events VALUES(?,?,?)", (self.library_key, event.event_id, encoded))
        for change in payload["changes"]:
            pair = (self.library_key, change["category_id"], change["asset_id"])
            if change["operation"] == "add":
                db.execute("INSERT OR IGNORE INTO category_member_adds VALUES(?,?,?,?)", (*pair, event.event_id))
            else:
                db.executemany("INSERT OR IGNORE INTO category_member_removes VALUES(?,?,?,?)", ((*pair, tag) for tag in change["tags"]))
            if self._live_tags(db, change["category_id"], change["asset_id"]):
                db.execute("INSERT OR IGNORE INTO category_memberships VALUES(?,?,?)", pair)
            else:
                db.execute("DELETE FROM category_memberships WHERE library_key=? AND category_id=? AND asset_id=?", pair)
        return True

    def _apply(self, db, event):
        payload = self._validate_event(event)
        return self._apply_category(db, event, payload) if event.entity_type == ENTITY_TYPE else self._apply_membership(db, event, payload)

    def apply_event(self, event, db=None):
        if db is not None:
            if not db.in_transaction:
                raise RuntimeError("接收分类事件必须在事务中运行")
            return self._apply(db, event)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._apply(connection, event)

    def _publish(self, db, entity_type, entity_id, payload):
        payload["revision"] = _event_hash(entity_type, entity_id, self.device_id, payload)
        event = Event.new(self.device_id, entity_type, entity_id, "append", payload,
                          event_id=payload["revision"], timestamp=payload["updated_at"])
        self._apply(db, event)
        db.execute("INSERT INTO outbox VALUES(?,?,?,?)", (event.event_id, self.library_key, _json(event.to_dict()), payload["updated_at"]))

    def save(self, data, category_id=None, expected_heads=None, resolve=False, db=None):
        """Write atomically; optional db joins an existing caller-owned transaction."""
        if db is not None:
            if not db.in_transaction:
                raise RuntimeError("写入必须在调用方事务中运行")
            return self._save(db, data, category_id, expected_heads, resolve)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._save(connection, data, category_id, expected_heads, resolve)

    def _save(self, db, data, category_id=None, expected_heads=None, resolve=False):
        if type(resolve) is not bool:
            raise ValueError("版本合并参数不正确")
        category_id = _identity(category_id) if category_id is not None else uuid.uuid4().hex
        expected = _heads(expected_heads) if expected_heads is not None else None
        current = self._get(db, category_id)
        heads = current["heads"] if current else []
        if current and expected is None or expected is not None and expected != heads:
            raise CategoryConflictError("分类已被修改，请保留草稿并重新载入最新版本")
        if len(heads) > 1 and not resolve:
            raise CategoryConflictError("分类存在并行版本，请查看各版本并合并保存")
        if len(heads) > 1 and (not isinstance(data, dict) or set(data) != DATA_FIELDS):
            raise ValueError("合并并行版本时必须提供完整分类内容")
        value = _data(data, current)
        if not value["archived"] and db.execute("SELECT 1 FROM category_entities WHERE library_key=? AND archived=0 AND name_key=? AND category_id<>? LIMIT 1",
            (self.library_key, _name_key(value["name"]), category_id)).fetchone():
            raise ValueError("已存在同名分类，请换一个名称")
        payload = dict(schema=1, library_key=self.library_key, data=value, parents=heads,
                       updated_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"))
        self._publish(db, ENTITY_TYPE, category_id, payload)
        return self._get(db, category_id)

    def assign(self, asset_ids, category_ids, remove=False, db=None):
        """Write atomically; optional db joins an existing caller-owned transaction."""
        if db is not None:
            if not db.in_transaction:
                raise RuntimeError("写入必须在调用方事务中运行")
            return self._assign(db, asset_ids, category_ids, remove)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._assign(connection, asset_ids, category_ids, remove)

    def _assign(self, db, asset_ids, category_ids, remove=False):
        if type(remove) is not bool:
            raise ValueError("分类移除参数不正确")
        assets = _ids(asset_ids, digest=True, allow_empty=False)
        categories = _ids(category_ids, allow_empty=False)
        if len(assets) * len(categories) > 1000:
            raise ValueError("一次最多调整 1000 个素材与分类关联，请分批操作")
        for asset_id in assets:
            if not db.execute("SELECT 1 FROM assets WHERE root_key=? AND asset_id=?", (self.library_key, asset_id)).fetchone():
                raise ValueError("有素材不在本机当前素材库中，请先同步或扫描")
        for category_id in categories:
            category = self._get(db, category_id)
            if category is None or category["archived"]:
                raise ValueError("有分类不存在或已归档，请刷新分类列表")
        changes = []
        for category_id in categories:
            for asset_id in assets:
                tags = self._live_tags(db, category_id, asset_id)
                if remove and tags or not remove and not tags:
                    changes.append(dict(category_id=category_id, asset_id=asset_id,
                                        operation="remove" if remove else "add", tags=tags if remove else []))
        if not changes:
            return {"changed": 0}
        payload = dict(schema=1, library_key=self.library_key, changes=changes,
                       updated_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"))
        self._publish(db, MEMBERSHIP_ENTITY_TYPE, uuid.uuid4().hex, payload)
        return {"changed": len(changes)}
