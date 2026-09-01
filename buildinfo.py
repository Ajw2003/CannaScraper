"""What this build is, and whether the source has moved on since.

The packaged app and the checkout it came from are two different things, and
nothing used to say so. An exe built before a fix landed serves the old code
happily and looks identical from the outside -- which is exactly how a search
bug that had already been fixed on the branch stayed in front of the user for
days.

So a build stamps itself: build.ps1 writes buildinfo.json into the source
directory, CannaCabana.spec bundles it, and `status()` compares it against the
checkout it names.

Read through paths.app(), never paths.seed(). This file describes the bundle,
so a copy of it in DATA_DIR outliving the bundle it describes would be a lie.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import paths

STAMP_NAME = "buildinfo.json"

#: Source files whose mtime means "the app would be different if rebuilt".
#: Everything the bundle actually carries: the Python and the web UI. Not
#: catalog.json or stores.json -- those are data the running app refreshes on
#: its own, and treating them as source would mark every build stale within a
#: day of itself.
_SOURCE_GLOBS = ("*.py", os.path.join("web", "*"))


def read() -> dict | None:
    """The stamp this build was made with, or None running from source.

    utf-8-sig, not utf-8: build.ps1 writes this with PowerShell 5.1's
    `Set-Content -Encoding utf8`, which prepends a BOM. Plain utf-8 leaves
    that byte in the string and json.load rejects it -- silently, from here,
    since a missing stamp and a malformed one both just mean "nothing to
    compare against" and neither should crash the app that reads it.
    """
    try:
        with open(paths.app(STAMP_NAME), encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _parse(ts: str | None) -> float | None:
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return None


def source_newest(source_dir: str) -> tuple[float | None, str | None]:
    """Newest mtime among the source files a rebuild would pick up.

    mtime rather than a git comparison on purpose: git is not guaranteed to be
    on the PATH of whoever runs the exe, and mtime catches an uncommitted edit
    as well as a new commit. The commit recorded in the stamp is captured at
    build time -- where git is available -- and is shown for context only.
    """
    import glob

    newest, which = None, None
    for pat in _SOURCE_GLOBS:
        for p in glob.glob(os.path.join(source_dir, pat)):
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if newest is None or m > newest:
                newest, which = m, os.path.basename(p)
    return newest, which


def status() -> dict:
    """Whether the running build is behind its source, and can be rebuilt.

    Running from source is never out of date with itself, and neither is a
    build whose source directory has gone (someone else's machine), so both
    report stale=False rather than nagging about something unactionable.
    """
    stamp = read() or {}
    src = stamp.get("source_dir") or ""
    out = {
        "frozen": paths.FROZEN,
        "built_at": stamp.get("built_at"),
        "commit": stamp.get("commit"),
        "branch": stamp.get("branch"),
        "dirty": stamp.get("dirty"),
        "source_dir": src,
        "source_exists": bool(src) and os.path.isdir(src),
        "changed": None,
        "age_hours": None,
        "stale": False,
        "rebuildable": False,
    }

    built = _parse(stamp.get("built_at"))
    if built is not None:
        out["age_hours"] = round((datetime.now(timezone.utc).timestamp()
                                  - built) / 3600.0, 1)

    if not (paths.FROZEN and built is not None and out["source_exists"]):
        return out

    out["rebuildable"] = (os.path.isfile(os.path.join(src, "build.ps1"))
                          and os.path.isfile(os.path.join(src, "rebuild.ps1"))
                          and os.path.isfile(os.path.join(
                              src, ".venv", "Scripts", "python.exe")))

    newest, which = source_newest(src)
    if newest is not None and newest > built:
        out["stale"] = True
        out["changed"] = which
    return out


def summary() -> str | None:
    """One line for the startup banner, or None when there is nothing to say."""
    s = status()
    if not s["frozen"] or s["age_hours"] is None:
        return None
    age = f"{s['age_hours'] / 24:.0f} days old" if s["age_hours"] >= 48 else \
          f"{s['age_hours']:.0f}h old"
    if not s["stale"]:
        return f"{age}, up to date with the source"
    tail = ("rebuild from the page, or run build.ps1" if s["rebuildable"]
            else f"source at {s['source_dir']} has changed")
    return f"{age} — OUT OF DATE ({s['changed']} is newer); {tail}"
