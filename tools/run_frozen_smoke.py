from pathlib import Path
import json
import shutil
import subprocess

root = Path(__file__).resolve().parents[1]
out = root / "build" / "verification" / "frozen-smoke.json"
data = root / "build" / "verification" / "frozen-data"
out.unlink(missing_ok=True)
shutil.rmtree(data, ignore_errors=True)
exe = root / "build" / "0.3.0" / "dist" / "素材协作" / "素材协作.exe"
result = subprocess.run([str(exe), "--data-dir", str(data), "--no-auto-sync", "--smoke-test", str(out)], cwd=exe.parent, timeout=30)
if result.returncode:
    raise SystemExit(result.returncode)
report = json.loads(out.read_text(encoding="utf-8"))
assert report["version"] == "0.3.0"
assert Path(report["ffmpeg"]).is_file()
assert Path(report["sqlite"]).is_file()
print(json.dumps(report, ensure_ascii=False, indent=2))
