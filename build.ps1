param(
    [string]$Version = "",
    [string]$Output = "dist",
    [string]$NasPublishPath = "",
    [string]$ReleaseNotes = "",
    [switch]$PublishOnly
)

$ErrorActionPreference = "Stop"
$taskBuildArgs = @((Join-Path $PSScriptRoot "tools\build_release.py"), "--output", $Output)
if ($Version) { $taskBuildArgs += @("--version", $Version) }
if ($NasPublishPath) { $taskBuildArgs += @("--publish", $NasPublishPath) }
if ($ReleaseNotes) { $taskBuildArgs += @("--notes", $ReleaseNotes) }
if ($PublishOnly) { $taskBuildArgs += "--publish-only" }
& python @taskBuildArgs
if ($LASTEXITCODE -ne 0) { throw "Build failed (exit $LASTEXITCODE)." }
