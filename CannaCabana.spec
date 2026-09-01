# PyInstaller spec for the distributable app.
#
#     .venv\Scripts\pyinstaller CannaCabana.spec
#
# build.ps1 does this plus the checks around it; run that instead unless you
# are debugging the bundle itself.
#
# onedir, not onefile: onefile re-extracts the whole bundle to a temp folder on
# every launch, which for ~60 MB is a visible delay before anything appears.

import os

block_cipher = None

HERE = os.path.abspath(SPECPATH)

# Read-only files that travel with the app. paths.APP_DIR resolves to the
# folder these land in, and paths.seed() copies the data ones into
# %LOCALAPPDATA%\CannaCabana on first run so they stay writable.
datas = [
    (os.path.join(HERE, "web"), "web"),
    (os.path.join(HERE, "catalog.json"), "."),
    (os.path.join(HERE, "stores.json"), "."),
    (os.path.join(HERE, "watchlist.txt"), "."),
]

# Optional: bundled so the app can open a public tunnel with no separate
# install. build.ps1 fetches it. Without it the app still serves local + LAN
# and says why the public URL is unavailable.
_cf = os.path.join(HERE, "cloudflared.exe")
if os.path.isfile(_cf):
    datas.append((_cf, "."))

# Written by build.ps1 right before this runs. A bare `pyinstaller
# CannaCabana.spec` (skipping build.ps1) still has to work, so this is
# optional -- buildinfo.status() already treats a missing stamp as "nothing
# to compare, not stale".
_buildinfo = os.path.join(HERE, "buildinfo.json")
if os.path.isfile(_buildinfo):
    datas.append((_buildinfo, "."))

hiddenimports = [
    # uvicorn picks these by string at runtime, so static analysis misses them.
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # Imported inside functions or only from __main__.
    "index_builder",
    # Reached only through index_builder / server, so listed here alongside
    # the rest rather than relied on transitively.
    "egress",
    "workqueue",
    "selftest",
    "tunnel",
    "jobs",
    "auth",
    "paths",
    "ratelimit",
    "buildinfo",
]

excludes = [
    # The browser backend and its ~350 MB of Chromium. fetchers.have_browser()
    # reports it absent and the UI drops the option.
    "playwright",
    "pyee",
    # db.export_csv uses the stdlib csv module now.
    "pandas",
    "numpy",
    "dateutil",
    # Installed in the dev venv, never imported by this project.
    "bs4",
    "soupsieve",
    "dotenv",
    # Not a server we ever run with --reload.
    "watchfiles",
    # Nothing here draws anything.
    "tkinter",
    "PIL",
]

a = Analysis(
    ["app.py"],
    pathex=[HERE],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CannaCabana",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # A console app on purpose: an index build takes 3 to 49 minutes and its
    # progress belongs somewhere you can watch it.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CannaCabana",
)
