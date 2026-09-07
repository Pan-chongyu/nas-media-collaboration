"""Decentralized event log used by the desktop clients.

The NAS is treated as an append-only SMB share.  Every device writes only to
``<root>/events/<device_id>`` and readers build their local state from the
event files.  No shared database file is opened over SMB.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Optional


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_component(value: str) -> bool:
    return bool(value) and bool(_SAFE_COMPONENT.fullmatch(value)) and value not in {".", ".."}


@dataclass(frozen=True)
class Event:
    """A single immutable synchronization record."""

    event_id: str
    device_id: str
    entity_type: str
    entity_id: str
    operation: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_utc_now)
    sequence: Optional[int] = None

    @classmethod
    def new(
        cls,
        device_id: str,
        entity_type: str,
        entity_id: str,
        operation: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        event_id: Optional[str] = None,
        timestamp: Optional[str] = None,
        sequence: Optional[int] = None,
    ) -> "Event":
        return cls(
            event_id=event_id or uuid.uuid4().hex,
            device_id=device_id,
            entity_type=entity_type,
            entity_id=entity_id,
            operation=operation,
            payload=dict(payload or {}),
            timestamp=timestamp or _utc_now(),
            sequence=sequence,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Event":
        required = ("event_id", "device_id", "entity_type", "entity_id", "operation")
        if any(not value.get(key) for key in required):
            raise ValueError("event is missing a required field")
        payload = value.get("payload", {})
        if not isinstance(payload, Mapping):
            raise ValueError("event payload must be an object")
        return cls(
            event_id=str(value["event_id"]),
            device_id=str(value["device_id"]),
            entity_type=str(value["entity_type"]),
            entity_id=str(value["entity_id"]),
            operation=str(value["operation"]),
            payload=dict(payload),
            timestamp=str(value.get("timestamp") or _utc_now()),
            sequence=value.get("sequence"),
        )


@dataclass(frozen=True, order=True)
class EventCursor:
    """The last event seen by a consumer."""

    timestamp: str
    device_id: str
    event_id: str

    @classmethod
    def from_event(cls, event: Event) -> "EventCursor":
        return cls(event.timestamp, event.device_id, event.event_id)

    @property
    def key(self):
        return (self.timestamp, self.device_id, self.event_id)

    def encode(self) -> str:
        raw = json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii")

    @classmethod
    def decode(cls, value: str | "EventCursor") -> "EventCursor":
        if isinstance(value, cls):
            return value
        try:
            raw = base64.urlsafe_b64decode(value.encode("ascii"))
            data = json.loads(raw.decode("utf-8"))
            return cls(str(data["timestamp"]), str(data["device_id"]), str(data["event_id"]))
        except (ValueError, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid event cursor") from exc


Cursor = EventCursor


def _event_key(event: Event) -> tuple[str, str, str]:
    return (event.timestamp, event.device_id, event.event_id)


def _events_dir(root: Path | str) -> Path:
    return Path(root) / "events"


def append_event(root: Path | str, event: Event, *, strict: bool = False) -> Optional[Path]:
    """Append *event* atomically and return its path.

    Re-appending the same event ID is idempotent.  SMB disconnects and access
    errors return ``None`` by default; callers that need an error can pass
    ``strict=True``.
    """

    if not isinstance(event, Event):
        raise TypeError("event must be an Event")
    if not _safe_component(event.device_id) or not _safe_component(event.event_id):
        raise ValueError("device_id and event_id must be safe path components")
    directory = _events_dir(root) / event.device_id
    destination = directory / f"{event.event_id}.json"
    lock = directory / f".{event.event_id}.lock"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # A short lock prevents two writers for the same ID from replacing one
        # another.  Stale locks are harmless and are removed after a timeout.
        lock_fd: Optional[int] = None
        for _ in range(100):
            try:
                lock_fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                time.sleep(0.01)
        if lock_fd is None:
            try:
                lock.unlink()
            except OSError:
                pass
            lock_fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(lock_fd)
        try:
            if destination.exists():
                try:
                    existing = Event.from_dict(json.loads(destination.read_text(encoding="utf-8")))
                    if existing.event_id == event.event_id:
                        if existing.to_dict() == event.to_dict():
                            return destination
                        raise ValueError("相同事件 ID 的内容冲突")
                except OSError:
                    pass
                except json.JSONDecodeError:
                    pass
            content = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            fd, temporary = tempfile.mkstemp(prefix=f".{event.event_id}.", suffix=".tmp", dir=str(directory))
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            finally:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
            return destination
        finally:
            try:
                lock.unlink()
            except OSError:
                pass
    except OSError:
        if strict:
            raise
        return None


def read_events(
    root: Path | str,
    after: Optional[EventCursor | str] = None,
    *,
    limit: Optional[int] = None,
    strict: bool = False,
    on_error=None,
    seen_ids: Optional[set[str]] = None,
) -> list[Event]:
    """Read valid events after *after*, in deterministic order."""

    try:
        base = _events_dir(root)
        if not base.exists():
            return []
        cursor = EventCursor.decode(after) if after is not None else None
        events: list[Event] = []
        seen: set[str] = set(seen_ids or ())
        for path in base.glob("*/*.json"):
            try:
                event = Event.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                if on_error:
                    on_error(path, exc)
                continue
            if event.event_id in seen:
                continue
            if cursor is not None and event.device_id == cursor.device_id and event.timestamp <= cursor.timestamp:
                continue
            seen.add(event.event_id)
            events.append(event)
        events.sort(key=_event_key)
        return events[:limit] if limit is not None else events
    except OSError:
        if strict:
            raise
        return []


def read_events_with_cursor(root: Path | str, after: Optional[EventCursor | str] = None, *, limit: Optional[int] = None, strict: bool = False) -> tuple[list[Event], Optional[EventCursor]]:
    events = read_events(root, after, limit=limit, strict=strict)
    return events, (EventCursor.from_event(events[-1]) if events else (EventCursor.decode(after) if after is not None else None))


def save_cursor(path: Path | str, cursor: Optional[EventCursor]) -> bool:
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if cursor is None:
            target.unlink(missing_ok=True)
            return True
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(cursor.encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        return True
    except OSError:
        return False


def load_cursor(path: Path | str) -> Optional[EventCursor]:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
        return EventCursor.decode(value) if value else None
    except (OSError, ValueError):
        return None


def merge_event_records(events: Iterable[Event], records: Optional[Mapping[str, Mapping[str, Any]]] = None) -> dict[str, dict[str, Any]]:
    """Merge events into a local record mapping without mutating the input."""

    result: dict[str, dict[str, Any]] = {str(key): dict(value) for key, value in (records or {}).items()}
    applied = {str(value.get("_sync_event_id")) for value in result.values() if value.get("_sync_event_id")}
    for event in sorted(events, key=_event_key):
        if event.event_id in applied:
            continue
        current = result.setdefault(event.entity_id, {})
        if current.get("_sync_timestamp") and event.timestamp <= str(current["_sync_timestamp"]):
            continue
        if event.operation.lower() in {"delete", "remove"}:
            current["_deleted"] = True
        else:
            current.update(dict(event.payload))
            current.pop("_deleted", None)
        current["_sync_event_id"] = event.event_id
        current["_sync_timestamp"] = event.timestamp
        current["_sync_device_id"] = event.device_id
        applied.add(event.event_id)
    return result


def merge_events_sqlite(connection: sqlite3.Connection, events: Iterable[Event]) -> None:
    """Persist merged records in a small client-local SQLite table."""

    connection.execute("CREATE TABLE IF NOT EXISTS sync_records (entity_id TEXT PRIMARY KEY, data_json TEXT NOT NULL)")
    existing = {row[0]: json.loads(row[1]) for row in connection.execute("SELECT entity_id, data_json FROM sync_records")}
    merged = merge_event_records(events, existing)
    connection.executemany("INSERT OR REPLACE INTO sync_records(entity_id, data_json) VALUES (?, ?)", ((key, json.dumps(value, ensure_ascii=False)) for key, value in merged.items()))
    connection.commit()


__all__ = ["Event", "EventCursor", "Cursor", "append_event", "read_events", "read_events_with_cursor", "save_cursor", "load_cursor", "merge_event_records", "merge_events_sqlite"]
