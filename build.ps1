# Build the distributable app.
#
#     .\build.ps1
#     .\build.ps1 -SkipCloudflared     # no public tunnel in this build
#     .\build.ps1 -Clean               # throw away dist\ and build\ first
#     .\build.ps1 -StopRunning         # kill a running copy that locks dist\
#
# Every step prints PASS or FAIL and the script stops at the first failure, so
# a green run means the exe in dist\CannaCabana\ actually started and passed
# its own self-test -- not merely that PyInstaller exited zero.

param(
    [switch]$SkipCloudflared,
    [switch]$Clean,
    [switch]$StopRunning
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

# --- 5. nothing holding the output open -------------------------------------
# A running copy keeps its .pyd files locked, and PyInstaller's failure for
# that is an eight-frame traceback ending in "Access is denied" -- which tells
# you nothing about the actual cause. Say it plainly instead.
Step-Start "no running copy locking dist\"
$target = Join-Path $Here "dist\CannaCabana"

# An Explorer window sitting in the output folder holds a handle on the
# directory itself. PyInstaller then deletes every file inside it and fails on
# the final rmdir, which reads as a permissions problem and is not one.
$explorers = @()
try {
    $shell = New-Object -ComObject Shell.Application
    foreach ($w in $shell.Windows()) {
        try {
            $loc = $w.Document.Folder.Self.Path
            if ($loc -eq $target -or $loc -like "$target\*") { $explorers += $w }
        } catch { }
    }
} catch { }
if ($explorers.Count -gt 0) {
    if ($StopRunning) {
        # Navigate away rather than closing the window; they get it back with
        # the Back button.
        foreach ($w in $explorers) { try { $w.Navigate((Split-Path $target)) } catch { } }
        Start-Sleep -Milliseconds 700
        Write-Host ""
        Write-Host "      moved $($explorers.Count) Explorer window(s) out of dist\CannaCabana"
        Write-Host ("      " + (" " * 40)) -NoNewline
    } else {
        Write-Host ""
        Write-Host "      An Explorer window is open in dist\CannaCabana." -ForegroundColor Yellow
        Write-Host "      It locks the folder. Close it, or re-run with -StopRunning."
        Write-Host ("      " + (" " * 40)) -NoNewline
        Step-Fail "output folder is in use by Explorer"
    }
}

$running = Get-Process CannaCabana -ErrorAction SilentlyContinue
if ($running -and -not $StopRunning) {
    Write-Host ""
    Write-Host "      CannaCabana.exe is running (PID $($running.Id -join ', '))." -ForegroundColor Yellow
    Write-Host "      It holds files in dist\ open, so the build cannot replace them."
    Write-Host "      Close that window (Ctrl-C in it), or re-run:  .\build.ps1 -StopRunning"
    Write-Host ("      " + (" " * 40)) -NoNewline
    Step-Fail "output folder is in use"
}
$consoleHosts = @()
if ($running) {
    # Capture the launcher before killing the app. Closing a console app does
    # not close the window it was launched into, and that window's working
    # directory is this folder -- which keeps it locked even though no file is
    # open. Restart Manager does not report that, so it has to be found here.
    foreach ($p in $running) {
        $parent = (Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)").ParentProcessId
        if ($parent) {
            $pp = Get-CimInstance Win32_Process -Filter "ProcessId=$parent" -ErrorAction SilentlyContinue
            if ($pp -and $pp.Name -in @("WindowsTerminal.exe", "cmd.exe",
                                        "powershell.exe", "pwsh.exe", "conhost.exe")) {
                $consoleHosts += $pp
            }
        }
    }
    $running | Stop-Process -Force
}

# Windows releases handles a moment after the holder lets go, and
# PyInstaller's first act is to rmdir this folder. Clear it here, with
# retries, so the build does not race that and fail on WinError 32.
$freed = $true
if (Test-Path $target) {
    $freed = $false
    foreach ($try in 1..20) {
        try { Remove-Item -Recurse -Force $target -ErrorAction Stop; $freed = $true; break }
        catch { Start-Sleep -Milliseconds 500 }
    }
    # Still stuck: the window the app was launched into is the usual reason.
    if (-not $freed -and $consoleHosts.Count -gt 0) {
        foreach ($ch in $consoleHosts) {
            Write-Host ""
            Write-Host "      closing the console it was launched from ($($ch.Name), pid $($ch.ProcessId))"
            try { Stop-Process -Id $ch.ProcessId -Force -ErrorAction Stop } catch { }
        }
        foreach ($try in 1..20) {
            Start-Sleep -Milliseconds 500
            if (-not (Test-Path $target)) { $freed = $true; break }
            try { Remove-Item -Recurse -Force $target -ErrorAction Stop; $freed = $true; break }
            catch { }
        }
        Write-Host ("      " + (" " * 40)) -NoNewline
    }
}
if (-not $freed) {
    Write-Host ""
    Write-Host "      Something still holds dist\CannaCabana open after 10s." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "      Most likely: the console window the app was launched from."
    Write-Host "      Closing the app does not close its window, and that window's"
    Write-Host "      working directory is this folder, which keeps it locked."
    Write-Host "      Close it and re-run. (Restart Manager will not report this"
    Write-Host "      one - a working-directory lock is not a file handle.)"
    Write-Host ""
    Write-Host "      Otherwise: an Explorer window inside it, or antivirus mid-scan."
    Write-Host ("      " + (" " * 40)) -NoNewline
    Step-Fail "could not clear the output folder"
}
Step-Pass $(if ($running) { "stopped the running app, dist\ is clear" }
            else { "nothing holding it" })

# --- 5b. product catalogue is current ---------------------------------------
# A packaged build ships whatever catalog.json happens to be sitting in this
# folder. Search reads that file straight off disk with no age gate (see
# catalog.load_catalog), which is what makes an offline install work at all --
# but it also means a catalogue that was already stale on the day of the build
# stays exactly that stale until someone clicks Refresh. Freshen it here so a
# new build starts current.
#
# Network failure is a warning, not a Step-Fail: with the age gate gone, a
# stale bundled catalogue is harmless -- search still answers, and the app's
# own startup hook tops it up later. A build should not fail because the site
# happened to be down.
Step-Start "product catalogue refreshed"
$catalogOut = & $Py -c "import catalog; catalog.refresh_catalog(verbose=False)" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "      could not refresh -- shipping the existing catalog.json" -ForegroundColor Yellow
    Write-Host "      ($($catalogOut | Select-Object -Last 1))"
    Write-Host ("      " + (" " * 40)) -NoNewline
    Step-Pass "kept existing catalogue (offline?)"
} else {
    Step-Pass "downloaded current"
}

# --- 5c. build stamp ---------------------------------------------------------
# What buildinfo.status() compares the checkout against at runtime, so a
# stale exe can say so instead of silently serving old code next to a source
# tree that has moved on. Written before PyInstaller so CannaCabana.spec picks
# it up; source_dir is $Here itself, since that is what a rebuild operates on.
Step-Start "build stamp written"
$commit = (& git rev-parse --short HEAD 2>$null)
$branch = (& git rev-parse --abbrev-ref HEAD 2>$null)
$dirty = $false
if ($LASTEXITCODE -eq 0) {
    $porcelain = (& git status --porcelain 2>$null)
    $dirty = [bool]$porcelain
}
$stamp = [ordered]@{
    built_at    = (Get-Date).ToUniversalTime().ToString("o")
    commit      = if ($commit) { $commit } else { $null }
    branch      = if ($branch) { $branch } else { $null }
    dirty       = $dirty
    source_dir  = $Here
}
$stamp | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $Here "buildinfo.json")
Step-Pass $(if ($commit) { "$branch @ $commit$(if ($dirty) {' (dirty)'})" } else { "no git -- commit/branch left blank" })

# --- 6. package -------------------------------------------------------------
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

# --- 7. helper next to the exe ----------------------------------------------
# Not a PyInstaller "data" file: those land in _internal\, and this one has to
# sit beside the exe where someone will actually see and double-click it.
Step-Start "password helper alongside the exe"
$Bat = Join-Path $Here "Set password.bat"
if (-not (Test-Path $Bat)) { Step-Fail "missing '$Bat'" }
Copy-Item $Bat (Join-Path $Here "dist\CannaCabana\") -Force
Step-Pass "Set password.bat copied"

# --- 8. it starts and proves itself -----------------------------------------
Step-Start "packaged app passes its self-test"
Write-Host ""
& $Exe --selftest
$selftest = $LASTEXITCODE
Write-Host ("      " + (" " * 40)) -NoNewline
if ($selftest -ne 0) { Step-Fail "$selftest check(s) failed" }
Step-Pass "all checks green"

# --- 9. report --------------------------------------------------------------
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
