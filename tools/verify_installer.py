"""Install to a fresh test directory, preserve the live installation, and verify playback."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import winreg

ROOT = Path(__file__).resolve().parents[1]
KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{7A8E0F4E-0A7D-4B4C-93F4-2C66DDE3B12A}_is1"


def registration():
    result = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as key:
            for index in range(winreg.QueryInfoKey(key)[1]):
                name, value, kind = winreg.EnumValue(key, index)
                result[name] = [value, kind]
    except FileNotFoundError:
        pass
    return result


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    version = re.search(r'^APP_VERSION = "([\d.]+)"', (ROOT / "main.py").read_text(encoding="utf-8"), re.M).group(1)
    installer = ROOT / "dist" / f"素材协作-{version}-setup.exe"
    verification = args.work_dir.resolve() / ("install-" + uuid.uuid4().hex)
    verification.mkdir(parents=True)
    target = verification / "application"
    before = registration()
    subprocess.run([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-", "/NORESTART",
                    "/NOCLOSEAPPLICATIONS", "/NORESTARTAPPLICATIONS", "/VERIFYINSTALL=1",
                    f"/DIR={target}", f"/LOG={verification / 'install.log'}"], check=True, timeout=120,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert registration() == before, "Verification changed the live installation registration"
    source = ROOT / "build" / version / "dist" / "素材协作"
    checked = 0
    for original in source.rglob("*"):
        if original.is_file():
            installed = target / original.relative_to(source)
            assert installed.is_file() and digest(installed) == digest(original), str(installed)
            checked += 1
    for extra in ([], ["--player"], ["--collaboration"]):
        subprocess.run([sys.executable, str(ROOT / "tools/run_frozen_smoke.py"),
                        "--exe", str(target / "素材协作.exe"), *extra], cwd=ROOT, check=True, timeout=90)
    report = {"version": version, "installer": str(installer), "sha256": digest(installer),
              "installed_to": str(target), "files_verified": checked,
              "live_registration_unchanged": True, "startup": "passed", "player": "passed", "collaboration": "passed"}
    destination = ROOT / "build/verification" / f"installer-{version}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
