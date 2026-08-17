"""Turn a set of observations into an HTML page and open it.

Nobody should have to read a CSV to answer "who has this in stock". The page
is self-contained (no CDN, no fonts, no JS) so it opens instantly from disk
and works offline.
"""

from __future__ import annotations

import html
import os
import webbrowser
from datetime import datetime

CSS = """
:root{--bg:#faf9f7;--fg:#1c1b19;--mut:#6b6864;--line:#e5e1db;--card:#fff;
      --ok:#1a7f4b;--okbg:#e8f5ee;--low:#a8650a;--lowbg:#fdf2e2;--none:#8a8681}
@media(prefers-color-scheme:dark){:root{--bg:#171614;--fg:#f0eee9;--mut:#a09c96;
      --line:#302e2a;--card:#201f1c;--ok:#5cc98d;--okbg:#14301f;--low:#e0a34a;
      --lowbg:#33260f;--none:#7a7671}}
*{box-sizing:border-box}
body{margin:0;padding:28px 20px 60px;background:var(--bg);color:var(--fg);
     font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:820px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--mut);font-size:13px;margin-bottom:22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
      overflow:hidden;margin-bottom:26px}
.hd{padding:13px 16px;border-bottom:1px solid var(--line);font-weight:600}
.hd small{font-weight:400;color:var(--mut)}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:11px;letter-spacing:.06em;text-transform:uppercase;
   color:var(--mut);padding:9px 16px;border-bottom:1px solid var(--line)}
td{padding:11px 16px;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:0}
.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pill{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;
      font-weight:600}
.s-ok{background:var(--okbg);color:var(--ok)}
.s-low{background:var(--lowbg);color:var(--low)}
.s-no{color:var(--none)}
.store{font-weight:600}
.city{color:var(--mut);font-size:13px}
.empty{padding:26px 16px;color:var(--mut)}
.stale{color:var(--low)}
footer{color:var(--mut);font-size:12px;text-align:center;margin-top:30px}
"""


def _age(hours: float | None) -> str:
    if hours is None:
        return "just now"
    if hours < 1 / 60:
        return "just now"
    if hours < 1:
        return f"{int(hours * 60)} min ago"
    if hours < 24:
        return f"{hours:.1f} h ago"
    return f"{hours / 24:.1f} days ago"


def _stock_cell(r: dict) -> str:
    qty, carried = r.get("api_stock"), r.get("carried")
    if not carried:
        return '<span class="s-no">not carried</span>'
    if qty is None:
        avail = r.get("available")
        return ('<span class="pill s-ok">in stock</span>' if avail
                else '<span class="s-no">sold out</span>')
    if qty <= 0:
        return '<span class="s-no">sold out</span>'
    cls = "s-ok" if qty >= 5 else "s-low"
    unit = "unit" if qty == 1 else "units"
    return f'<span class="pill {cls}">{qty} {unit}</span>'


def _money(v) -> str:
    try:
        return f"${float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


def build(rows: list[dict], query: str = "", age_hours: float | None = None,
          location: str = "") -> str:
    """Render rows (dicts from db) grouped by product, best stock first."""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r.get("sku"), r.get("title"), r.get("size")), []).append(r)

    parts = []
    for (sku, title, size), items in groups.items():
        items.sort(key=lambda r: (
            -(r.get("api_stock") or 0),
            0 if r.get("available") else 1,
            r.get("distance_km") if r.get("distance_km") is not None else 9e9,
        ))
        have = [r for r in items if r.get("available")]
        show_dist = any(r.get("distance_km") is not None for r in items)

        body = []
        for r in items:
            dist = (f'{r["distance_km"]:.1f} km'
                    if r.get("distance_km") is not None else "—")
            body.append(
                "<tr>"
                f'<td><div class="store">{html.escape(str(r.get("store_name") or ""))}</div>'
                f'<div class="city">{html.escape(str(r.get("city") or ""))}</div></td>'
                f'<td>{_stock_cell(r)}</td>'
                + (f'<td class="n">{dist}</td>' if show_dist else "")
                + f'<td class="n">{_money(r.get("price"))}</td>'
                f'<td class="n">{_money(r.get("member_price"))}</td>'
                "</tr>"
            )

        parts.append(f"""
<div class="card">
  <div class="hd">{html.escape(str(title or "?"))}
    <small>&nbsp;{html.escape(str(size or ""))} &middot; SKU {html.escape(str(sku or "?"))}
    &middot; in stock at {len(have)} of {len(items)} checked</small></div>
  <table><thead><tr><th>Store</th><th>Stock</th>
  {'<th class="n">Distance</th>' if show_dist else ''}
  <th class="n">Price</th><th class="n">Member</th></tr></thead>
  <tbody>{''.join(body) or '<tr><td class="empty" colspan=5>No results.</td></tr>'}</tbody></table>
</div>""")

    stale = age_hours is not None and age_hours > 12
    sub = []
    if query:
        sub.append(f"Search: <strong>{html.escape(query)}</strong>")
    if location:
        sub.append(f"Near {html.escape(location)}")
    sub.append(f'Data <span class="{"stale" if stale else ""}">{_age(age_hours)}</span>')

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stock — {html.escape(query or 'Canna Cabana')}</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>Where it's in stock</h1>
<div class="sub">{' &middot; '.join(sub)}</div>
{''.join(parts) or '<div class="card"><div class="empty">Nothing to show.</div></div>'}
<footer>Canna Cabana stock lookup &middot; generated {datetime.now():%Y-%m-%d %H:%M}</footer>
</div></body></html>"""


def write_and_open(rows, path="report.html", open_browser=True, **kw) -> str:
    with open(path, "w", encoding="utf-8") as f:
        f.write(build(rows, **kw))
    if open_browser:
        try:
            webbrowser.open(f"file://{os.path.abspath(path)}")
        except Exception:
            pass
    return path
