$ErrorActionPreference = "Stop"
& python (Join-Path $PSScriptRoot "run_frozen_smoke.py") @args
if ($LASTEXITCODE -ne 0) { throw "Frozen smoke test failed: $LASTEXITCODE" }
