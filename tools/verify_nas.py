"""Exercise production media read-only and publish its actual index events."""

from contextlib import closing
import json
from pathlib import Path
import time

from indexer import open_index
from library import LibraryService
from main import CONFIG_PATH, LOCAL_DATA, load_settings, store_settings


def main():
    output = Path("build/verification")
    output.mkdir(parents=True, exist_ok=True)
    settings = load_settings(CONFIG_PATH)
    store_settings(CONFIG_PATH, settings)
    service = LibraryService(LOCAL_DATA, settings["nas_root"], settings["sync_root"], settings["device_id"])
    last_report = time.monotonic()

    def progress(value):
        nonlocal last_report
        if time.monotonic() - last_report >= 5 or value["status"] != "running":
            print(json.dumps({key: value[key] for key in ("status", "done", "changed", "errors")}), flush=True)
            last_report = time.monotonic()

    start = time.monotonic()
    first = service.scan(progress=progress)
    if first["status"] != "done":
        raise RuntimeError(first)
    duration = round(time.monotonic() - start, 2)
    second = service.scan(progress=progress)
    if second["status"] != "done":
        raise RuntimeError(second)
    published = []
    while service.overview()["pending"]:
        result = service.sync_once()
        published.append(result)
        print(json.dumps(dict(publish=result)), flush=True)
        if result["status"] != "done" or result["sent"] == 0:
            raise RuntimeError(result)
    thumbnails = []
    for kind in ("image", "video"):
        for item in service.page(media_type=kind, limit=3)[0]:
            result = service.thumbnail(item["asset_id"], item["file_hash"])
            thumbnails.append(dict(kind=kind, **result))
            print(json.dumps(dict(thumbnail=kind, status=result["status"])), flush=True)
    peer = LibraryService(output / "peer", settings["nas_root"], settings["sync_root"], "verification-peer")
    received = []
    while True:
        result = peer.sync_once()
        received.append(result)
        print(json.dumps(dict(peer=result)), flush=True)
        if result["status"] != "done":
            raise RuntimeError(result)
        if result["received"] == 0:
            break
    count = service.page(limit=1)[1]
    peer_count = peer.page(limit=1)[1]
    if count != peer_count:
        raise RuntimeError(f"Peer count mismatch: {count} != {peer_count}")
    with closing(open_index(service.db_path)) as db:
        types = {row[0]: row[1] for row in db.execute("SELECT media_type,count(*) FROM assets GROUP BY media_type")}
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    report = dict(first_scan=first, repeat_scan=second, first_seconds=duration,
                  types=types, published=published, thumbnails=thumbnails,
                  peer_sync=received, count=count, peer_count=peer_count, integrity=integrity)
    (output / "nas.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(count=count, peer_count=peer_count, integrity=integrity, first_seconds=duration)), flush=True)


if __name__ == "__main__":
    main()
