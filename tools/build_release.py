"""Build a complete Windows installer, or publish an already verified installer."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "素材协作"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(args: list[str | Path]) -> None:
    subprocess.run([str(arg) for arg in args], cwd=ROOT, check=True)


def find_file(candidates: list[Path | None], label: str) -> Path:
    for path in candidates:
        if path and path.is_file():
            return path.resolve()
    raise RuntimeError(f"未找到 {label}，请按 README 配置构建依赖")


def env_path(name: str) -> Path | None:
    value = os.getenv(name)
    return Path(value) if value else None


def which(name: str) -> Path | None:
    value = shutil.which(name)
    return Path(value) if value else None


def build(version: str, output: Path) -> Path:
    local = Path(os.environ["LOCALAPPDATA"])
    ffmpeg = find_file([env_path("FFMPEG_PATH"), local / "Programs/ffmpeg/bin/ffmpeg.exe",
                        which("ffmpeg")], "FFmpeg")
    mpv = find_file([env_path("MPV_PATH"), ROOT / "build/dependencies/mpv/mpv.exe",
                    which("mpv")], "mpv")
    compiler = find_file([env_path("ISCC_PATH"), which("ISCC"), local / "InnoSetup/ISCC.exe",
                          local / "Programs/Inno Setup 6/ISCC.exe",
                          Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe"),
                          Path("C:/Program Files/Inno Setup 6/ISCC.exe")], "Inno Setup")
    if not (compiler.parent / "Languages/ChineseSimplified.isl").is_file():
        raise RuntimeError("Inno Setup 缺少 ChineseSimplified.isl 中文语言文件")
    from importlib.metadata import version as package_version
    for package, expected in (("PyInstaller", "6.22.2"), ("Pillow", "12.3.0"), ("tkinterdnd2", "0.4.3")):
        if package_version(package) != expected:
            raise RuntimeError(f"构建需要 {package}=={expected}")
    target = ROOT / "build" / version
    target.mkdir(parents=True, exist_ok=True)
    arguments = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
                 "--name", APP_NAME, "--distpath", str(target / "dist"),
                 "--workpath", str(target / "work"), "--specpath", str(target),
                 "--add-binary", f"{ffmpeg};tools", "--add-binary", f"{mpv};tools",
                 "--add-data", f"{ROOT / 'docs/licenses'};licenses", "--collect-data", "tkinterdnd2"]
    companion = mpv.parent / "d3dcompiler_43.dll"
    if companion.is_file():
        arguments += ["--add-binary", f"{companion};tools"]
    run(arguments + [str(ROOT / "main.py")])
    application = target / "dist" / APP_NAME
    for relative in (f"{APP_NAME}.exe", "_internal/tools/ffmpeg.exe", "_internal/tools/mpv.exe"):
        if not (application / relative).is_file():
            raise RuntimeError(f"应用构建缺少文件：{relative}")
    run([compiler, f"/DMyAppVersion={version}", f"/DAppSourceDir={application}",
         f"/O{output}", ROOT / "installer" / f"{APP_NAME}.iss"])
    installer = output / f"{APP_NAME}-{version}-setup.exe"
    if not installer.is_file():
        raise RuntimeError("安装包未生成")
    report = {"version": version, "installer": str(installer), "sha256": digest(installer),
              "bytes": installer.stat().st_size,
              "components": {"ffmpeg": digest(ffmpeg), "mpv": digest(mpv)}}
    (target / "build-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return installer


def publish(installer: Path, destination: Path, version: str, notes: str) -> None:
    if not destination.is_dir():
        raise RuntimeError(f"发布目录不可访问：{destination}")
    expected = digest(installer)
    final = destination / installer.name
    manifest_path = destination / "manifest.json"
    old_manifest = manifest_path.read_bytes() if manifest_path.exists() else None
    if old_manifest:
        current = json.loads(old_manifest.decode("utf-8-sig"))
        if tuple(map(int, current["version"].split("."))) > tuple(map(int, version.split("."))):
            raise RuntimeError("NAS 已有更新版本，拒绝回退更新清单")
    if final.exists() and digest(final) != expected:
        raise RuntimeError("NAS 已有同版本不同内容的安装包，拒绝覆盖，请提升版本号")
    staging = destination / (installer.name + "." + uuid.uuid4().hex + ".tmp")
    manifest_staging = destination / ("manifest." + uuid.uuid4().hex + ".tmp")
    try:
        if not final.exists():
            shutil.copyfile(installer, staging)
            if digest(staging) != expected:
                raise RuntimeError("NAS 安装包 SHA-256 校验失败")
            staging.rename(final)
        if digest(final) != expected:
            raise RuntimeError("NAS 安装包校验失败")
        manifest = {"version": version, "url": str(final), "sha256": expected,
                    "mandatory": False, "notes": notes}
        manifest_staging.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if old_manifest and manifest_path.read_bytes() != old_manifest:
            raise RuntimeError("发布期间更新清单发生变化，请重新检查后发布")
        os.replace(manifest_staging, manifest_path)
    finally:
        staging.unlink(missing_ok=True)
        manifest_staging.unlink(missing_ok=True)
    print(f"已发布并校验：{final}\nSHA-256: {expected}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version")
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--publish", type=Path)
    parser.add_argument("--notes", default="支持直接导入 Word 拍摄脚本；按编号和时间段生成分镜，保留镜头动作、人物台词及拍摄信息，可预览选材并确认分类")
    parser.add_argument("--publish-only", action="store_true")
    args = parser.parse_args()
    match = re.search(r'^APP_VERSION = "(\d+\.\d+\.\d+)"', (ROOT / "main.py").read_text(encoding="utf-8"), re.M)
    if not match:
        raise RuntimeError("无法从 main.py 读取版本")
    version = match.group(1)
    if args.version and args.version != version:
        raise RuntimeError(f"指定版本与源码版本 {version} 不一致")
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.mkdir(parents=True, exist_ok=True)
    installer = output / f"{APP_NAME}-{version}-setup.exe"
    if args.publish_only:
        if not args.publish or not installer.is_file():
            raise RuntimeError("PublishOnly 需要发布目录和已经构建的安装包")
    else:
        installer = build(version, output)
    if args.publish:
        publish(installer, args.publish, version, args.notes)


if __name__ == "__main__":
    main()
