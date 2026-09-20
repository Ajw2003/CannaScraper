# Rebuild the exe and relaunch it, run from a fresh console spawned by the
# app itself so it can hand off the port.
#
# PyInstaller has to delete the running exe's own _internal\*.pyd files,
# which the running process holds open -- so this cannot happen in-process.
# The server that requested the rebuild exits itself right after spawning
# this, and this script waits for that PID before touching dist\.
#
#     .\rebuild.ps1 -WaitForPid 12345
#
# On a failed build the old exe is untouched (build.ps1 stops at the first
# FAIL before PyInstaller runs), so the fallback is just relaunching it and
# leaving this console open with the error.

param(
    [int]$WaitForPid = 0
)

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here

Write-Host ("=" * 74)
Write-Host "  Rebuilding CannaCabana"
Write-Host ("=" * 74)

if ($WaitForPid -gt 0) {
    Write-Host "  waiting for the running app (pid $WaitForPid) to exit..."
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id $WaitForPid -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 300
    }
    if (Get-Process -Id $WaitForPid -ErrorAction SilentlyContinue) {
        Write-Host "  still running after 60s -- stopping it."
        Stop-Process -Id $WaitForPid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
}

$exe = Join-Path $Here "dist\CannaCabana\CannaCabana.exe"

& (Join-Path $Here "build.ps1") -StopRunning
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "  Build failed. The previous app has not been touched." -ForegroundColor Red
    Write-Host "  Relaunching it now; fix the error above and rebuild when ready."
    Write-Host ""
    if (Test-Path $exe) { Start-Process $exe }
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host ""
Write-Host "  Build succeeded -- starting the new build."
Start-Process $exe
Start-Sleep -Seconds 2
