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
import math
import os
import re
import urllib.parse
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
        # 'district' / 'eastlake' for Calgary-area stores. In delivery mode the
        # site prices these as their hub rather than themselves -- which is why
        # config.AGE_GATE_STATE keeps delivery off. Kept so we can tell which
        # stores would be affected if that ever regresses.
        "hub_id": (s.get("hub_id") or "").strip().lower(),
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


# --- "near me" --------------------------------------------------------------
# Every one of the 225 stores carries latitude/longitude, so ranking by
# distance costs nothing but arithmetic. This is what makes "check 10 stores"
# mean the 10 closest rather than the first 10 alphabetically.

def distance_km(lat1, lng1, lat2, lng2) -> float:
    """Great-circle distance. stdlib only."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def nearest(stores: list[dict], lat: float, lng: float,
            n: int | None = None) -> list[dict]:
    """`stores` sorted by distance from (lat, lng), each with `distance_km`."""
    out = []
    for s in stores:
        if s.get("latitude") is None or s.get("longitude") is None:
            continue
        out.append({**s, "distance_km": round(
            distance_km(lat, lng, s["latitude"], s["longitude"]), 1)})
    out.sort(key=lambda s: s["distance_km"])
    return out[:n] if n else out


def geocode(place: str) -> tuple[float, float] | None:
    """Resolve a place name to coordinates.

    Accepts "51.05,-114.07" directly, otherwise asks Nominatim (OpenStreetMap)
    -- free, no API key. Results are cached in geocode.json so a repeat lookup
    costs nothing and we stay well inside their usage policy.
    """
    place = (place or "").strip()
    if not place:
        return None

    m = re.match(r"^\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*$", place)
    if m:
        return float(m.group(1)), float(m.group(2))

    cache: dict = {}
    if os.path.exists(config.GEOCODE_CACHE):
        try:
            with open(config.GEOCODE_CACHE, encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            cache = {}

    key = place.lower()
    if key in cache:
        return tuple(cache[key])                      # type: ignore[return-value]

    q = urllib.parse.urlencode({"q": place, "format": "json", "limit": 1,
                                "countrycodes": "ca"})
    req = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?{q}",
        headers={"User-Agent": config.NOMINATIM_UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            hits = json.loads(r.read().decode("utf-8", errors="ignore"))
    except Exception:
        return None
    if not hits:
        return None

    coords = (float(hits[0]["lat"]), float(hits[0]["lon"]))
    cache[key] = list(coords)
    try:
        with open(config.GEOCODE_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=1)
    except OSError:
        pass
    return coords


def resolve_location(place: str | None) -> tuple[float, float] | None:
    """Where is 'near me'? Explicit place, else config.HOME."""
    if place:
        return geocode(place)
    if config.HOME:
        return geocode(config.HOME) if isinstance(config.HOME, str) else tuple(config.HOME)
    return None


if __name__ == "__main__":
    import sys

    if "--near" in sys.argv:
        i = sys.argv.index("--near")
        place = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
        top = config.DEFAULT_TOP
        if "--top" in sys.argv:
            top = int(sys.argv[sys.argv.index("--top") + 1])
        prov = None
        if "--province" in sys.argv:
            prov = sys.argv[sys.argv.index("--province") + 1]

        loc = resolve_location(place)
        if not loc:
            print(f"Could not locate {place!r}. Try a city, a postal code, "
                  f'or "lat,lng".')
            raise SystemExit(2)
        print(f"Location: {loc[0]:.4f}, {loc[1]:.4f}\n")
        near = nearest(get_stores(province=prov if prov is not None else ""),
                       loc[0], loc[1], top)
        print(f"  {'km':>6}  {'store':<28} {'city':<18} province")
        for s in near:
            print(f"  {s['distance_km']:>6.1f}  {s['name'][:28]:<28} "
                  f"{s['city'][:18]:<18} {s['province']}")
        raise SystemExit(0)

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
