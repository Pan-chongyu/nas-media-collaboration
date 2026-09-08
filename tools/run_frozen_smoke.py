"""Verify the frozen app with isolated data and no host media tools on PATH."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

root = Path(__file__).resolve().parents[1]
version = re.search(r'^APP_VERSION = "([\d.]+)"', (root / "main.py").read_text(encoding="utf-8"), re.M).group(1)
parser = argparse.ArgumentParser()
parser.add_argument("--exe", type=Path, default=root / "build" / version / "dist" / "素材协作" / "素材协作.exe")
group = parser.add_mutually_exclusive_group()
group.add_argument("--player", action="store_true")
group.add_argument("--collaboration", action="store_true")
arguments = parser.parse_args()
exe = arguments.exe.resolve()
verification = root / "build" / "verification" / ("frozen-" + uuid.uuid4().hex)
verification.mkdir(parents=True)
out = verification / "smoke.json"
data = verification / "data"
environment = dict(os.environ)
environment.update(PATH=str(Path(os.environ["SystemRoot"]) / "System32"),
                   LOCALAPPDATA=str(verification / "local"), APPDATA=str(verification / "roaming"))
environment.pop("MPV_PATH", None)
environment.pop("FFMPEG_PATH", None)
mode = "--player-smoke-test" if arguments.player else "--collaboration-smoke-test" if arguments.collaboration else "--smoke-test"
result = subprocess.run([str(exe), "--data-dir", str(data), "--no-auto-sync", mode, str(out)],
                        cwd=verification, timeout=60, env=environment,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
if result.returncode:
    raise SystemExit(result.returncode)
report = json.loads(out.read_text(encoding="utf-8"))
if arguments.collaboration:
    assert report["ok"] and len(report["checks"]) == 5 and report["dnd"], report
elif arguments.player:
    assert report["ok"] and report["closed"], report
    assert len(report["checks"]) == 7, report
else:
    assert report["version"] == version, report
    assert report["dnd"], "Native file drag component was not bundled"
    assert Path(report["sqlite"]).is_file(), report
    assert Path(report["ffmpeg"]).is_relative_to(exe.parent), report
    assert Path(report["ffmpeg"]).is_file(), report
if not arguments.collaboration:
    assert Path(report["mpv"]).is_file(), report
    assert Path(report["mpv"]).is_relative_to(exe.parent), report
print(json.dumps(report, ensure_ascii=False, indent=2))
