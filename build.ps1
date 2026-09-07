param(
    [string]$Version = "0.3.1",
    [string]$Output = "dist"
)

$ErrorActionPreference = "Stop"
python -m PyInstaller --noconfirm --clean --windowed --name "素材协作-$Version" --distpath $Output main.py
if (Test-Path "$env:LOCALAPPDATA\Programs\ffmpeg\bin\ffplay.exe") {
    New-Item -ItemType Directory -Force -Path "$Output\素材协作-$Version\tools" | Out-Null
    Copy-Item "$env:LOCALAPPDATA\Programs\ffmpeg\bin\ffplay.exe" "$Output\素材协作-$Version\tools\ffplay.exe" -Force
}
Write-Host "构建完成：$Output\素材协作-$Version"
