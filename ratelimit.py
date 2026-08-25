"""How close we actually get to the server's request budget.

Every response carries `X-RateLimit-Limit` and `X-RateLimit-Remaining`, and we
used to discard both. That left "60 per minute" as a number the server
advertises and nobody had checked -- no 429 has ever been observed, so where it
actually refuses is unknown.

Recording the low-water mark costs nothing: no extra requests, just reading two
headers we already receive.

**Read the numbers with care.** The counter does not decrement one per request.
Measured drops across endpoints ranged from under 1 to about 3 per call, which
suggests a rolling window and probably a budget shared between the catalogue
search and the per-store price scan. So treat the minimum as "the closest we
were seen to get", not as an exact count of anything.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import paths

STATE_PATH = Path(paths.data("ratelimit.json"))

_lock = threading.Lock()
_run: dict = {}


# A 429 that arrives with plenty of budget left is not the budget refusing us.
# Store 528 has produced two of those; it also produces HTTP 500s, and has
# never once succeeded on this endpoint. Counting those as rate limiting would
# report pressure that does not exist.
BUDGET_FLOOR = 5


def _blank() -> dict:
    return {"samples": 0, "min_remaining": None, "limit": None,
            # Real budget exhaustion: 429 with the allowance spent.
            "throttled": 0,
            # 429 with budget to spare -- a specific store or endpoint
            # refusing, not us being too fast.
            "refused_other": 0,
            "refused_by_store": {}}


_run = _blank()


def _pick(headers, name: str):
    """Header lookup that survives being handed a plain dict.

    urllib gives a case-insensitive message object, but api_fetcher converts
    it to a dict before we see it, and that is case-sensitive.
    """
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except AttributeError:
        return None
    if value is not None:
        return value
    wanted = name.lower()
    try:
        for key, value in headers.items():
            if key.lower() == wanted:
                return value
    except AttributeError:
        pass
    return None


def start_run() -> None:
    with _lock:
        _run.update(_blank())


def classify_429(headers) -> bool:
    """Is this 429 the budget refusing us, or something else?

    True means genuine rate limiting -- the allowance is spent, so backing off
    and waiting for the window is the right response. False means we were
    refused while holding plenty of budget, which is a property of whatever we
    asked rather than of how fast we asked it, and waiting will not help.
    """
    remaining = _pick(headers, "X-RateLimit-Remaining")
    if remaining is None:
        return True                       # no evidence; assume the cautious case
    try:
        return int(remaining) <= BUDGET_FLOOR
    except (TypeError, ValueError):
        return True


def observe(headers, status: int = 200, store_id: str | None = None) -> None:
    """Fold one response's headers into the current run."""
    with _lock:
        if status == 429:
            if classify_429(headers):
                _run["throttled"] += 1
            else:
                _run["refused_other"] += 1
                if store_id:
                    by = _run["refused_by_store"]
                    by[str(store_id)] = by.get(str(store_id), 0) + 1

        limit = _pick(headers, "X-RateLimit-Limit")
        if limit is not None:
            try:
                _run["limit"] = int(limit)
            except (TypeError, ValueError):
                pass

        remaining = _pick(headers, "X-RateLimit-Remaining")
        if remaining is None:
            return
        try:
            left = int(remaining)
        except (TypeError, ValueError):
            return

        _run["samples"] += 1
        if _run["min_remaining"] is None or left < _run["min_remaining"]:
            _run["min_remaining"] = left


def snapshot() -> dict:
    with _lock:
        return dict(_run)


def summarize() -> str:
    """One line for the end of a run, or "" if the server told us nothing."""
    s = snapshot()
    if not s["samples"] or s["min_remaining"] is None:
        return ""
    low, limit = s["min_remaining"], s["limit"]
    head = f"rate limit: got within {low}"
    if limit:
        head += f" of {limit}"
    head += f" at the tightest point over {s['samples']} calls"
    if s["throttled"]:
        head += f", {s['throttled']} genuinely throttled (429, budget spent)"
    if s["refused_other"]:
        who = ", ".join(f"store {k}" for k in sorted(s["refused_by_store"]))
        head += (f", {s['refused_other']} 429 with budget to spare"
                 + (f" ({who})" if who else "") + " - not rate limiting")
    return head


# --- history ---------------------------------------------------------------
# Persisted so the picture builds up across runs and restarts rather than
# vanishing with the process.

def _load() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def record(kind: str) -> dict:
    """Fold the finished run into the stored history. Returns that entry."""
    s = snapshot()
    if not s["samples"]:
        return {}
    history = _load()
    entry = history.setdefault(kind, {"lowest_remaining": None, "runs": 0,
                                      "throttled": 0, "limit": None})
    entry.setdefault("refused_other", 0)
    entry.setdefault("refused_by_store", {})
    entry["runs"] += 1
    entry["throttled"] += s["throttled"]
    entry["refused_other"] += s["refused_other"]
    for sid, n in s["refused_by_store"].items():
        entry["refused_by_store"][sid] = entry["refused_by_store"].get(sid, 0) + n
    entry["limit"] = s["limit"] or entry.get("limit")
    low = s["min_remaining"]
    if low is not None and (entry["lowest_remaining"] is None
                            or low < entry["lowest_remaining"]):
        entry["lowest_remaining"] = low
    try:
        STATE_PATH.write_text(json.dumps(history, indent=2) + "\n",
                              encoding="utf-8")
    except OSError:
        pass
    return entry


def history() -> dict:
    return _load()
