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
.pill.elite{background:#f3e6c8;color:#7a5a12;margin-left:8px;font-size:11px;
      letter-spacing:.04em;text-transform:uppercase}
@media(prefers-color-scheme:dark){.pill.elite{background:#3a2f12;color:#e6c88a}}
.save{color:var(--ok);white-space:nowrap}
.save small{opacity:.75}
tr.muted td{opacity:.5}

/* Show/hide stores without the product, in pure CSS -- a checkbox plus the
   sibling combinator, so the page needs no JavaScript and stays one portable
   file. #showall must precede .card in the DOM for `~` to reach the rows. */
#showall{position:absolute;opacity:0;pointer-events:none}
.toggle{display:inline-flex;align-items:center;gap:8px;cursor:pointer;
  font-size:13px;color:var(--mut);border:1px solid var(--line);
  background:var(--card);border-radius:20px;padding:6px 14px;margin-bottom:22px;
  user-select:none}
.toggle:hover{color:var(--fg)}
.toggle .box{width:14px;height:14px;border:1.5px solid var(--mut);
  border-radius:4px;display:inline-block;position:relative;flex:none}
#showall:checked ~ .toggle .box{background:var(--ok);border-color:var(--ok)}
#showall:checked ~ .toggle .box::after{content:"";position:absolute;left:4px;
  top:1px;width:4px;height:8px;border:solid #fff;border-width:0 2px 2px 0;
  transform:rotate(45deg)}
#showall:checked ~ .toggle .on{display:inline}
#showall:checked ~ .toggle .off{display:none}
.toggle .on{display:none}

tr.muted{display:none}
#showall:checked ~ .card tr.muted{display:table-row}
tr.hint td{color:var(--mut);font-size:13px;padding:12px 16px}
#showall:checked ~ .card tr.hint{display:none}
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
    txt = (r.get("stock_text") or "").lower()
    if txt == "not checked" or (carried is None and qty is None):
        return '<span class="s-no">not checked</span>'
    if not carried:
        return '<span class="s-no">not in stock</span>'
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


def _tier(r: dict) -> tuple[str, float | None]:
    """Which discount tier applies. ELITE and member are mutually exclusive."""
    elite = r.get("api_elite_price")
    member = r.get("member_price") or r.get("api_member_price")
    if r.get("is_elite") and elite:
        return "ELITE", elite
    if member:
        return "Member", member
    if elite:
        return "ELITE", elite
    return "", None


def _savings(r: dict) -> str:
    market = r.get("price")
    _, deal = _tier(r)
    try:
        if market and deal and float(deal) < float(market):
            m, d = float(market), float(deal)
            return f"−${m - d:.2f} <small>({100 * (m - d) / m:.0f}%)</small>"
    except (TypeError, ValueError):
        pass
    return "—"


def build(rows: list[dict], query: str = "", age_hours: float | None = None,
          location: str = "", checked: int | None = None) -> str:
    """Render rows (dicts from db) grouped by product, best stock first."""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r.get("sku"), r.get("title"), r.get("size")), []).append(r)

    parts = []
    hidden_total = [0]          # list so the per-card loop can add to it
    for (sku, title, size), items in groups.items():
        items.sort(key=lambda r: (
            -(r.get("api_stock") or 0),
            0 if r.get("available") else 1,
            r.get("distance_km") if r.get("distance_km") is not None else 9e9,
        ))
        have = [r for r in items if r.get("available")]
        show_dist = any(r.get("distance_km") is not None for r in items)

        is_elite = any(r.get("is_elite") for r in items)
        tier_label = "ELITE" if is_elite else "Member"

        body = []
        for r in items:
            dist = (f'{r["distance_km"]:.1f} km'
                    if r.get("distance_km") is not None else "—")
            _, deal = _tier(r)
            none_here = not r.get("available")
            body.append(
                f'<tr class="{"muted" if none_here else ""}">'
                f'<td><div class="store">{html.escape(str(r.get("store_name") or ""))}</div>'
                f'<div class="city">{html.escape(str(r.get("city") or ""))}</div></td>'
                f'<td>{_stock_cell(r)}</td>'
                + (f'<td class="n">{dist}</td>' if show_dist else "")
                + f'<td class="n">{_money(r.get("price"))}</td>'
                f'<td class="n">{_money(deal)}</td>'
                f'<td class="n save">{_savings(r)}</td>'
                "</tr>"
            )

        # A card whose rows are all hidden would look broken, so leave a line
        # explaining what was collapsed.
        n_hidden = len(items) - len(have)
        if n_hidden:
            hidden_total[0] += n_hidden
            noun = "store" if n_hidden == 1 else "stores"
            body.append(
                f'<tr class="hint"><td colspan="{5 + (1 if show_dist else 0)}">'
                f'{n_hidden} more {noun} checked &mdash; none in stock. '
                f'Use <em>Show stores without it</em> above to list them.'
                f'</td></tr>')

        badge = ('<span class="pill elite">ELITE members only</span>'
                 if is_elite else "")
        cols = 5 + (1 if show_dist else 0)
        # Count stores looked at, not rows returned: stores with none of the
        # product are included in `items` precisely so this reads honestly.
        n_checked = checked if checked is not None else len(items)
        summary = (f"in stock at {len(have)} of {n_checked} checked"
                   if have else f"not in stock at any of {n_checked} checked")

        parts.append(f"""
<div class="card">
  <div class="hd">{html.escape(str(title or "?"))} {badge}
    <small>&nbsp;{html.escape(str(size or ""))} &middot; SKU {html.escape(str(sku or "?"))}
    &middot; {summary}</small></div>
  <table><thead><tr><th>Store</th><th>Stock</th>
  {'<th class="n">Distance</th>' if show_dist else ''}
  <th class="n">Market</th><th class="n">{tier_label}</th><th class="n">You save</th></tr></thead>
  <tbody>{''.join(body) or f'<tr><td class="empty" colspan={cols}>No results.</td></tr>'}</tbody></table>
</div>""")

    stale = age_hours is not None and age_hours > 12
    sub = []
    if query:
        sub.append(f"Search: <strong>{html.escape(query)}</strong>")
    if location:
        sub.append(f"Near {html.escape(location)}")
    sub.append(f'Data <span class="{"stale" if stale else ""}">{_age(age_hours)}</span>')

    n_hidden = hidden_total[0]
    toggle = ""
    if n_hidden:
        noun = "store" if n_hidden == 1 else "stores"
        toggle = (
            '<input type="checkbox" id="showall">'
            '<label class="toggle" for="showall"><span class="box"></span>'
            f'<span class="off">Show {n_hidden} {noun} without it</span>'
            f'<span class="on">Hide {n_hidden} {noun} without it</span>'
            '</label>')

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stock — {html.escape(query or 'Canna Cabana')}</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>Where it's in stock</h1>
<div class="sub">{' &middot; '.join(sub)}</div>
{toggle}
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
