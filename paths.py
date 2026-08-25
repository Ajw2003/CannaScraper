"""Where the app reads from, and where it writes to.

Two roots, because a packaged build cannot write next to its own executable and
must not ship a 172 MB database:

  APP_DIR   Read-only files that travel with the app: the web UI, plus the
            catalogue and store seeds. Inside the PyInstaller bundle when
            frozen.
  DATA_DIR  Everything we write: history.db, settings.json, the caches. Under
            %LOCALAPPDATA% when frozen, so it survives replacing the app folder.

In a source checkout both are the project directory, so a git clone behaves
exactly as it did before this file existed.

Nothing here imports from the project, so config.py can import it freely.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

#: True when running from a PyInstaller bundle.
FROZEN = bool(getattr(sys, "frozen", False))

_HERE = Path(__file__).resolve().parent

if FROZEN:
    # onedir puts bundled data next to the exe; _MEIPASS covers onefile too.
    APP_DIR = Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent)
    _local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    DATA_DIR = Path(_local) / "CannaCabana"
else:
    APP_DIR = _HERE
    DATA_DIR = _HERE

# An escape hatch for testing the packaged layout without packaging, and for
# anyone who wants their data on another drive.
_override = os.environ.get("CANNACABANA_DATA")
if _override:
    DATA_DIR = Path(_override).expanduser().resolve()

DATA_DIR.mkdir(parents=True, exist_ok=True)


def data(name: str) -> str:
    """A path we may write to."""
    return str(DATA_DIR / name)


def app(name: str) -> str:
    """A path that ships with the app and is never written to."""
    return str(APP_DIR / name)


def seed(name: str) -> str:
    """A writable copy of a bundled file, copied in on first run.

    catalog.json and stores.json ship with the app so that search works before
    the first index build. Both are also rewritten in place when they expire,
    so they cannot be read straight out of the bundle.
    """
    dest = DATA_DIR / name
    if not dest.exists():
        src = APP_DIR / name
        if src.exists() and src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
    return str(dest)
