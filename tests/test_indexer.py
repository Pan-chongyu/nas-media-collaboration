from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from indexer import count_records, iter_scan, load_records, open_index, record_file, root_key, scan_files, upsert_records


class IndexerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_scan_media_and_skip_generated_files(self) -> None:
        (self.root / "中文项目" / "采访.mp4").parent.mkdir(parents=True)
        (self.root / "中文项目" / "采访.mp4").write_bytes(b"video")
        (self.root / "中文项目" / "封面.JPG").write_bytes(b"image")
        (self.root / "中文项目" / "声音.wav").write_bytes(b"audio")
        (self.root / "中文项目" / ".hidden.mp4").write_bytes(b"hidden")
        (self.root / "中文项目" / "~$draft.mp4").write_bytes(b"temp")
        (self.root / "代理").mkdir()
        (self.root / "代理" / "preview.mp4").write_bytes(b"proxy")
        (self.root / "缩略图").mkdir()
        (self.root / "缩略图" / "thumb.jpg").write_bytes(b"thumb")
        records = scan_files(self.root)
        expected = ["中文项目/封面.JPG", "中文项目/声音.wav", "中文项目/采访.mp4"]
        self.assertEqual([r.relative_path for r in records], sorted(expected, key=str.casefold))
        self.assertEqual({record.media_type for record in records}, {"image", "audio", "video"})
        self.assertEqual(records[0].asset_id, scan_files(self.root)[0].asset_id)

    def test_upsert_is_idempotent_and_detects_change(self) -> None:
        source = self.root / "clip.mp4"
        source.write_bytes(b"one")
        first = scan_files(self.root)
        connection = open_index(":memory:")
        self.assertEqual(upsert_records(connection, first), 1)
        self.assertEqual(len(load_records(connection)), 1)
        original_id = first[0].asset_id
        original_hash = first[0].file_hash

        self.assertEqual(upsert_records(connection, scan_files(self.root)), 0)
        self.assertEqual(len(load_records(connection)), 1)
        self.assertEqual(connection.total_changes, 1)
        source.write_bytes(b"two with another size")
        os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 1_000_000))
        changed = scan_files(self.root)[0]
        self.assertEqual(changed.asset_id, original_id)
        self.assertNotEqual(changed.file_hash, original_hash)
        upsert_records(connection, [changed])
        stored = load_records(connection)[0]
        self.assertEqual(stored.size, changed.size)
        self.assertEqual(stored.file_hash, changed.file_hash)
        connection.close()

    def test_query_and_invalid_directory(self) -> None:
        self.assertEqual(scan_files(self.root / "does-not-exist"), [])
        (self.root / "A.MP3").write_bytes(b"audio")
        connection = open_index(":memory:")
        upsert_records(connection, scan_files(self.root))
        self.assertEqual(len(load_records(connection, "a.mp")), 1)
        self.assertEqual(load_records(connection, "missing"), [])
        connection.close()

    def test_inaccessible_directory_is_skipped(self) -> None:
        with patch("indexer.os.walk", side_effect=PermissionError("Access denied")):
            self.assertEqual(scan_files(self.root), [])

    def test_mtime_change_is_a_new_version_with_same_id(self) -> None:
        source = self.root / "片段.mp4"
        source.write_bytes(b"same size")
        before = scan_files(self.root)[0]
        os.utime(source, ns=(source.stat().st_atime_ns, before.mtime_ns + 10_000_000))
        after = scan_files(self.root)[0]
        self.assertEqual(before.size, after.size)
        self.assertEqual(before.asset_id, after.asset_id)
        self.assertNotEqual(before.file_hash, after.file_hash)

    def test_roots_and_same_names_are_isolated(self):
        for folder in ("a", "b"):
            (self.root / folder).mkdir()
            (self.root / folder / "采访.mp4").write_bytes(b"video")
        a, b = scan_files(self.root / "a")[0], scan_files(self.root / "b")[0]
        self.assertNotEqual(a.asset_id, b.asset_id)
        with closing(open_index(":memory:")) as db:
            upsert_records(db, [a, b])
            self.assertEqual(count_records(db), 2)
            self.assertEqual(count_records(db, root=self.root / "a"), 1)
        self.assertIsNone(record_file(self.root / "b" / "采访.mp4", self.root / "a"))

    def test_pagination_and_literal_search(self):
        for i in range(8):
            (self.root / f"file_{i:02}.mp4").write_bytes(b"video")
        (self.root / "other.mp3").write_bytes(b"audio")
        db = open_index(":memory:")
        try:
            upsert_records(db, scan_files(self.root))
            page1 = load_records(db, root=self.root, limit=3)
            page2 = load_records(db, root=self.root, limit=3, offset=3)
            self.assertFalse({x.asset_id for x in page1} & {x.asset_id for x in page2})
            self.assertEqual(count_records(db, media_type="audio"), 1)
            self.assertEqual(count_records(db, "%"), 0)
            self.assertEqual(count_records(db, "file_"), 8)
        finally:
            db.close()

    def test_stream_cancel_and_inaccessible_root(self):
        for i in range(10):
            (self.root / f"{i}.mp4").write_bytes(b"video")
        stop = threading.Event()
        stream = iter_scan(self.root, stop_event=stop)
        next(stream)
        stop.set()
        self.assertEqual(list(stream), [])
        with self.assertRaises(OSError):
            list(iter_scan(self.root / "missing"))

    def test_caller_transaction_can_rollback(self):
        (self.root / "clip.mp4").write_bytes(b"video")
        db = open_index(":memory:")
        try:
            with self.assertRaises(ValueError), db:
                upsert_records(db, scan_files(self.root), commit=False)
                raise ValueError("outbox insertion failed")
            self.assertEqual(count_records(db), 0)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
