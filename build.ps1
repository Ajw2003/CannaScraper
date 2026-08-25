# Build the distributable app.
#
#     .\build.ps1
#     .\build.ps1 -SkipCloudflared     # no public tunnel in this build
#     .\build.ps1 -Clean               # throw away dist\ and build\ first
#
# Every step prints PASS or FAIL and the script stops at the first failure, so
# a green run means the exe in dist\CannaCabana\ actually started and passed
# its own self-test -- not merely that PyInstaller exited zero.

param(
    [switch]$SkipCloudflared,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $Here ".venv\Scripts\python.exe"
$Step = 0
$Failed = 0

function Step-Start($name) {
    $script:Step++
    Write-Host ("{0,2}. {1}" -f $script:Step, $name) -NoNewline
    Write-Host (" " * [Math]::Max(1, 46 - $name.Length)) -NoNewline
}

function Step-Pass($note) {
    Write-Host "PASS" -ForegroundColor Green -NoNewline
    if ($note) { Write-Host "   $note" } else { Write-Host "" }
}

function Step-Fail($note) {
    Write-Host "FAIL" -ForegroundColor Red -NoNewline
    if ($note) { Write-Host "   $note" } else { Write-Host "" }
    $script:Failed++
    throw $note
}

Write-Host ("=" * 74)
Write-Host "  Building CannaCabana"
Write-Host ("=" * 74)
Write-Host "  source : $Here"
Write-Host ""

# --- 1. the venv actually runs ---------------------------------------------
# .venv\pyvenv.cfg records a home path from before the venv was moved into
# CannaScraper\, so "the folder exists" is not evidence that it works.
Step-Start "virtualenv runs"
if (-not (Test-Path $Py)) {
    Step-Fail "no .venv - run: python -m venv .venv"
}
$ver = & $Py -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
if ($LASTEXITCODE -ne 0) { Step-Fail "the venv python will not start" }
Step-Pass "Python $ver"

# --- 2. dependencies --------------------------------------------------------
Step-Start "dependencies installed"
& $Py -m pip install --quiet --disable-pip-version-check -r (Join-Path $Here "requirements.txt")
if ($LASTEXITCODE -ne 0) { Step-Fail "pip install failed" }
& $Py -m pip install --quiet --disable-pip-version-check pyinstaller
if ($LASTEXITCODE -ne 0) { Step-Fail "could not install pyinstaller" }
Step-Pass "runtime deps + pyinstaller"

# --- 3. cloudflared ---------------------------------------------------------
# Official Cloudflare release. Without it the app still serves local and LAN;
# it just cannot open a public URL.
Step-Start "cloudflared.exe present"
$Cf = Join-Path $Here "cloudflared.exe"
if ($SkipCloudflared) {
    Step-Pass "skipped - this build will be local/LAN only"
} elseif (Test-Path $Cf) {
    $mb = [Math]::Round((Get-Item $Cf).Length / 1MB, 1)
    Step-Pass "already here ($mb MB)"
} elseif (Get-Command cloudflared -ErrorAction SilentlyContinue) {
    # Already installed on this machine; copy it in rather than re-downloading.
    # The recipient of the build will not have it, so it still has to ship.
    Copy-Item (Get-Command cloudflared).Source $Cf
    $mb = [Math]::Round((Get-Item $Cf).Length / 1MB, 1)
    Step-Pass "copied from your install ($mb MB)"
} else {
    $url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    Write-Host ""
    Write-Host "      downloading $url"
    try {
        Invoke-WebRequest -Uri $url -OutFile $Cf -UseBasicParsing
    } catch {
        Step-Fail "download failed: $_"
    }
    $mb = [Math]::Round((Get-Item $Cf).Length / 1MB, 1)
    Write-Host ("      " + (" " * 40)) -NoNewline
    Step-Pass "downloaded ($mb MB)"
}

# --- 4. clean ---------------------------------------------------------------
Step-Start "output folders clear"
foreach ($d in @("dist", "build")) {
    $p = Join-Path $Here $d
    if ((Test-Path $p) -and $Clean) { Remove-Item -Recurse -Force $p }
}
Step-Pass $(if ($Clean) { "removed dist\ and build\" } else { "reusing build cache (-Clean to force)" })

# --- 5. package -------------------------------------------------------------
Step-Start "PyInstaller bundle"
Push-Location $Here
try {
    & $Py -m PyInstaller --noconfirm --log-level WARN (Join-Path $Here "CannaCabana.spec")
    if ($LASTEXITCODE -ne 0) { Step-Fail "PyInstaller exited $LASTEXITCODE" }
} finally {
    Pop-Location
}
$Exe = Join-Path $Here "dist\CannaCabana\CannaCabana.exe"
if (-not (Test-Path $Exe)) { Step-Fail "no exe at $Exe" }
Step-Pass "built"

# --- 6. it starts and proves itself -----------------------------------------
Step-Start "packaged app passes its self-test"
Write-Host ""
& $Exe --selftest
$selftest = $LASTEXITCODE
Write-Host ("      " + (" " * 40)) -NoNewline
if ($selftest -ne 0) { Step-Fail "$selftest check(s) failed" }
Step-Pass "all checks green"

# --- 7. report --------------------------------------------------------------
Step-Start "size"
$size = (Get-ChildItem (Join-Path $Here "dist\CannaCabana") -Recurse -File |
         Measure-Object -Property Length -Sum).Sum
$sizeMb = [Math]::Round($size / 1MB, 1)
Step-Pass "$sizeMb MB"

Write-Host ""
Write-Host ("-" * 74)
Write-Host "  Done. The distributable is:"
Write-Host "    $(Join-Path $Here 'dist\CannaCabana')"
Write-Host ""
Write-Host "  Zip that whole folder to hand it to someone. On first run it"
Write-Host "  prints an admin password - that is the only time it is shown."
Write-Host ("-" * 74)
