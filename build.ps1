param(
    [string]$Version = "0.1.0",
    [string]$Output = "dist"
)

$ErrorActionPreference = "Stop"
python -m PyInstaller --noconfirm --clean --windowed --name "素材协作-$Version" --distpath $Output main.py
Write-Host "构建完成：$Output\素材协作-$Version"
