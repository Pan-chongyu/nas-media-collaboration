import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sync_events import (
    Event,
    EventCursor,
    append_event,
    load_cursor,
    merge_event_records,
    merge_events_sqlite,
    read_events,
    read_events_with_cursor,
    save_cursor,
)


class SyncEventsTests(unittest.TestCase):
    def test_append_and_read_are_sorted_and_support_chinese_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            events = [
                Event.new("device-b", "asset", "2", "update", {"标题": "中文"}, timestamp="2026-01-01T00:00:01.000Z", event_id="b"),
                Event.new("device-a", "asset", "1", "create", {"name": "A"}, timestamp="2026-01-01T00:00:00.000Z", event_id="a"),
            ]
            for event in events:
                self.assertIsNotNone(append_event(root, event))
            self.assertEqual([event.event_id for event in read_events(root)], ["a", "b"])
            payload = json.loads((root / "events" / "device-b" / "b.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["payload"]["标题"], "中文")

    def test_duplicate_event_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            event = Event.new("device", "asset", "x", "create", {"value": 1}, event_id="same")
            first = append_event(root, event)
            second = append_event(root, event)
            self.assertEqual(first, second)
            self.assertEqual(len(read_events(root)), 1)

    def test_concurrent_writers_on_different_devices(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            events = [Event.new(f"device-{i % 4}", "asset", str(i), "create", event_id=f"id-{i}") for i in range(40)]
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda event: append_event(root, event), events))
            self.assertEqual(len(read_events(root)), len(events))

    def test_cursor_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for i in range(3):
                append_event(root, Event.new("device", "asset", str(i), "create", event_id=f"id-{i}", timestamp=f"2026-01-01T00:00:0{i}Z"))
            first, cursor = read_events_with_cursor(root, limit=2)
            self.assertEqual(len(first), 2)
            self.assertIsNotNone(cursor)
            path = root / "cursor.txt"
            self.assertTrue(save_cursor(path, cursor))
            self.assertEqual(load_cursor(path), cursor)
            self.assertEqual([event.entity_id for event in read_events(root, load_cursor(path))], ["2"])

    def test_merge_is_idempotent_and_sqlite_compatible(self):
        events = [
            Event.new("d", "asset", "x", "create", {"name": "one"}, event_id="1", timestamp="2026-01-01T00:00:00Z"),
            Event.new("d", "asset", "x", "update", {"name": "two"}, event_id="2", timestamp="2026-01-01T00:00:01Z"),
        ]
        merged = merge_event_records(events + [events[1]])
        self.assertEqual(merged["x"]["name"], "two")
        connection = sqlite3.connect(":memory:")
        merge_events_sqlite(connection, events)
        self.assertEqual(json.loads(connection.execute("SELECT data_json FROM sync_records WHERE entity_id='x'").fetchone()[0])["name"], "two")
        connection.close()

    def test_late_event_with_earlier_timestamp_is_not_lost(self):
        with tempfile.TemporaryDirectory() as temp:
            later = Event.new("device-b", "asset", "new", "update", timestamp="2026-06-01T00:00:00Z")
            earlier = Event.new("device-a", "asset", "old", "update", timestamp="2026-01-01T00:00:00Z")
            append_event(temp, later)
            _, cursor = read_events_with_cursor(temp)
            append_event(temp, earlier)
            self.assertEqual([e.event_id for e in read_events(temp, cursor)], [earlier.event_id])
            self.assertEqual([e.event_id for e in read_events(temp, seen_ids={later.event_id})], [earlier.event_id])

    def test_same_id_conflict_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            event = Event.new("device", "asset", "x", "update", {"name": "original"}, event_id="same")
            append_event(temp, event)
            conflict = Event.new("device", "asset", "x", "update", {"name": "other"}, event_id="same")
            with self.assertRaises(ValueError):
                append_event(temp, conflict)
            self.assertEqual(read_events(temp)[0].payload["name"], "original")

    def test_concurrent_same_event_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            event = Event.new("device", "asset", "x", "update", {"name": "same"})
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda _: append_event(temp, event, strict=True), range(20)))
            self.assertEqual(len(set(results)), 1)
            self.assertEqual(len(read_events(temp)), 1)

    def test_corrupt_events_and_wrong_device_are_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            valid = Event.new("device", "asset", "x", "update")
            location = append_event(temp, valid)
            location.write_text("broken", encoding="utf-8")
            errors = []
            self.assertEqual(read_events(temp, on_error=lambda *args: errors.append(args)), [])
            self.assertEqual(len(errors), 1)

    def test_old_event_does_not_replace_current_record(self):
        old = Event.new("d", "asset", "x", "update", {"name": "old"}, timestamp="2026-01-01T00:00:00Z")
        new = Event.new("d", "asset", "x", "update", {"name": "new"}, timestamp="2026-06-01T00:00:00Z")
        records = merge_event_records([new])
        self.assertEqual(merge_event_records([old], records)["x"]["name"], "new")


if __name__ == "__main__":
    unittest.main()
