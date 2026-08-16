"""Store registry.

Canna Cabana embeds its full store list in the store-locator page as a series
of JavaScript assignments:

    currentStoreData = { ... };
    currentStoreData.has_delivery = false;
    currentStoreData.has_pickup  = false;
    window.stores["<handle>"] = currentStoreData;

The data is in JS, not the DOM, so an HTML parser is the wrong tool. We scan
for the object literals and brace-match them (a lazy regex truncates on the
nested `address` / `hours_periods` objects).
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

import config

_ASSIGN = re.compile(r"currentStoreData\s*=\s*\{")
_TAIL = re.compile(
    r"currentStoreData\.(has_delivery|has_pickup)\s*=\s*(true|false)"
    r"|window\.stores\[[\"']([^\"']+)[\"']\]"
)


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="ignore")


def _match_object(text: str, start: int) -> tuple[str, int]:
    """Return the JSON object beginning at `start` (index of '{') and the index
    just past its closing brace. Respects string literals and escapes."""
    depth, i, in_str, esc = 0, start, False, False
    while i < len(text):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1], i + 1
        i += 1
    raise ValueError("unbalanced braces in store object")


def parse_stores(html: str) -> list[dict]:
    """Extract every store object from the locator page HTML."""
    stores, seen = [], set()

    for m in _ASSIGN.finditer(html):
        brace = html.index("{", m.start())
        try:
            raw, end = _match_object(html, brace)
            obj = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            continue

        # The handle and the has_* flags follow the literal; grab them from the
        # short window of text between this object and the next assignment.
        window = html[end : end + 400]
        for t in _TAIL.finditer(window):
            flag, val, handle = t.group(1), t.group(2), t.group(3)
            if flag:
                obj[flag] = val == "true"
            elif handle:
                obj.setdefault("window_handle", handle)

        sid = str(obj.get("store_id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        stores.append(obj)

    return stores


def _normalize(s: dict) -> dict:
    addr = s.get("address") or {}
    return {
        "store_id": str(s.get("store_id") or ""),
        "handle": s.get("handle") or s.get("window_handle") or "",
        "name": s.get("title") or "",
        "street": addr.get("street1") or "",
        "city": addr.get("city") or "",
        "province": addr.get("province") or "",
        "zip": addr.get("zip") or "",
        "latitude": addr.get("latitude"),
        "longitude": addr.get("longitude"),
        "phone": s.get("phone") or "",
        "has_delivery": s.get("has_delivery"),
        "has_pickup": s.get("has_pickup"),
    }


def get_stores(province: str | None = None, refresh: bool = False,
               limit: int | None = None) -> list[dict]:
    """All stores in `province`, cached to disk. The registry changes rarely."""
    province = province if province is not None else config.PROVINCE

    if not refresh and os.path.exists(config.STORES_CACHE):
        with open(config.STORES_CACHE, encoding="utf-8") as f:
            allstores = json.load(f)
    else:
        allstores = [_normalize(s) for s in parse_stores(_fetch(config.LOCATOR_URL))]
        with open(config.STORES_CACHE, "w", encoding="utf-8") as f:
            json.dump(allstores, f, indent=1)

    out = [s for s in allstores if not province or s["province"] == province]
    out.sort(key=lambda s: (s["city"], s["name"]))

    limit = limit if limit is not None else config.MAX_STORES
    return out[:limit] if limit else out


if __name__ == "__main__":
    import sys

    refresh = "--refresh" in sys.argv
    every = get_stores(province="", refresh=refresh)
    print(f"total stores parsed : {len(every)}")
    provinces: dict[str, int] = {}
    for s in every:
        provinces[s["province"]] = provinces.get(s["province"], 0) + 1
    for p, n in sorted(provinces.items(), key=lambda kv: -kv[1]):
        print(f"  {p or '(blank)':<24} {n}")

    scoped = get_stores()
    print(f"\n{config.PROVINCE}: {len(scoped)} stores")
    for s in scoped[:5]:
        print(f"  [{s['store_id']:>5}] {s['name']} — {s['city']} ({s['latitude']}, {s['longitude']})")
