$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$out = Join-Path $root "build\verification\frozen-smoke.json"
$data = Join-Path $root "build\verification\frozen-data"
Remove-Item -LiteralPath $out -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $data -Recurse -Force -ErrorAction SilentlyContinue
$exe = Join-Path $root "build\0.3.0\dist\素材协作\素材协作.exe"
$info = [System.Diagnostics.ProcessStartInfo]::new()
$info.FileName = $exe
$info.WorkingDirectory = (Split-Path $exe -Parent)
$info.UseShellExecute = $false
$info.CreateNoWindow = $true
$info.Arguments = "--data-dir `"$data`" --no-auto-sync --smoke-test `"$out`""
$process = [System.Diagnostics.Process]::Start($info)
$process.WaitForExit()
if ($process.ExitCode -ne 0) { throw "冻结版退出码：$($process.ExitCode)" }
Get-Content -LiteralPath $out
