"""Browser test of the additions made on top of the original page on the Pages site.

Serves ROOT (a folder holding CannaScraper/ with site/ files and data/ from the
gh-pages branch) and walks the page in Chromium: card count and price, data
age line, browse with an empty box, "view" links, Back to search, remembered
province, the failed-stores notice (Saskatchewan's file edited in flight), and
no sideways scroll at phone width. Screenshots go to SHOTS.

    python3 ci/pages_ux_check.py ROOT SHOTS

See docs/plans/restore-original-page.md, "Additions from the first static site".

Data-independent: expectations come from the page's own /api/provinces and
/api/search responses at run time, not hard-coded live-data facts. See
docs/4-systems/ci-checks.md, "page-additions".
"""
import functools, http.server, json, os, threading, sys
from playwright.sync_api import sync_playwright

# CHROME_PATH overrides which Chromium binary Playwright launches. Unset, we
# fall back to the sandbox's pre-fetched build if present, else Playwright's
# own bundled Chromium (no executable_path -- `playwright install` handles it).
_SANDBOX_CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
CHROME = os.environ.get("CHROME_PATH") or (
    _SANDBOX_CHROME if os.path.exists(_SANDBOX_CHROME) else None
)

ROOT = sys.argv[1]
H = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT)
class Q(H.func):
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8971), functools.partial(Q, directory=ROOT))
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:8971/CannaScraper/"
SHOTS = sys.argv[2]
fails = []
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond: fails.append(msg)

with sync_playwright() as p:
    b = p.chromium.launch(**({"executable_path": CHROME} if CHROME else {}))
    ctx = b.new_context(viewport={"width": 1100, "height": 900})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.route("https://cdn.shopify.com/**", lambda r: r.abort())
    pg.goto(URL); pg.wait_for_timeout(2500)

    provinces = pg.evaluate("async () => (await (await fetch('/api/provinces')).json())")
    default_province = provinces["default"]
    print("default province (from /api/provinces):", default_province)

    age = pg.inner_text("#age"); print("age line:", age)
    check(f"{default_province} stock updated" in age, "age line names the default province")
    n = len(pg.query_selector_all("#plist .p"))
    check(n == 50, f"empty box browses: {n} cards on load")
    first = pg.inner_text("#plist .p >> nth=0").replace("\n", " | "); print("first card:", first)
    check(" stores" in first or " store " in first, "card shows store count")
    check("lowest $" in first and " at " in first, "card shows 'lowest $X at <store>'")
    print("more:", pg.inner_text("#more").replace("\n", " | "))
    pg.screenshot(path=f"{SHOTS}/ux-landing.png")

    pg.select_option("#prov", "Saskatchewan"); pg.wait_for_timeout(1500)
    check("Saskatchewan stock updated" in pg.inner_text("#age"), "age line follows province change")
    pg.fill("#q", "blue dream"); pg.wait_for_timeout(1200)
    cards = pg.query_selector_all("#plist .p"); print("blue dream cards:", len(cards))
    api_total = pg.evaluate("""async () => {
        const r = await fetch('/api/search?' + new URLSearchParams(
            {q: 'blue dream', limit: 50, offset: 0, province: 'Saskatchewan',
             stocked_only: true, category: ''}));
        return (await r.json()).total;
    }""")
    print("blue dream /api/search total:", api_total)
    check(len(cards) == min(api_total, 50),
          f"search 'blue dream' in Saskatchewan matches /api/search total ({len(cards)} vs {api_total})")
    pg.screenshot(path=f"{SHOTS}/ux-search.png")

    pg.fill("#q", ""); pg.wait_for_timeout(1200)
    pg.click("#moreb"); pg.wait_for_timeout(1200)
    n_before = len(pg.query_selector_all("#plist .p"))
    deep = pg.query_selector_all("#plist .p")[70]
    sku = deep.get_attribute("data-sku")
    deep.click(); pg.wait_for_timeout(1500)
    top = pg.evaluate("document.querySelector('#out').getBoundingClientRect().top")
    vh = pg.evaluate("innerHeight"); check(-5 < top < vh * 0.6, f"product in view (top={top:.0f} of {vh})")
    links = pg.query_selector_all("#out a.src")
    check(len(links) > 0 and "cannacabana.com/products/" in links[0].get_attribute("href"),
          f"store rows have 'view' links ({len(links)})")
    pg.screenshot(path=f"{SHOTS}/ux-product.png")
    pg.click("#back"); pg.wait_for_timeout(1500)
    n2 = len(pg.query_selector_all("#plist .p"))
    check(n2 == n_before, f"back restores the list at the same length ({n2} vs {n_before} before opening)")
    vis = pg.evaluate(f"""(() => {{ const r = document.querySelector('#plist .p[data-sku="{sku}"]').getBoundingClientRect();
                         return r.top > 0 && r.bottom < innerHeight; }})()""")
    check(vis, "back scrolls to the card that was opened")

    pg.reload(); pg.wait_for_timeout(2500)
    check(pg.eval_on_selector("#prov", "e => e.value") == "Saskatchewan", "province remembered after reload")

    # price order and the lowest-price-at-a-store line (user's request, 2026-09-28).
    # Expectations come from the page's own /api/search, not fixed data.
    import re
    def api(extra, province="Saskatchewan"):
        return pg.evaluate("""async ([extra, province]) => {
            const r = await fetch('/api/search?' + new URLSearchParams(Object.assign(
                {q: '', limit: 50, offset: 0, province: province,
                 stocked_only: true, category: ''}, extra)));
            return (await r.json()).products;
        }""", [extra, province])
    CARD = re.compile(r"lowest \$([0-9.,]+) at (.+?)(?: \((?:member|Elite)\))?$")
    def card_lowest(t):
        m = CARD.search(t.strip().split("\n")[-1])
        return (float(m.group(1).replace(",", "")), m.group(2)) if m else None
    def froms():
        out = []
        for t in pg.eval_on_selector_all("#plist .p", "els => els.map(e => e.innerText)"):
            c = card_lowest(t)
            out.append(c[0] if c else None)
        return out
    def ordered(vals, sign):
        pr = [v for v in vals if v is not None]
        nones_last = vals[len(pr):] == [None] * (len(vals) - len(pr))
        return nones_last and all(sign * (b - a) >= 0 for a, b in zip(pr, pr[1:]))
    pg.select_option("#prov", "Saskatchewan"); pg.wait_for_timeout(1500)
    pg.fill("#q", "gummies"); pg.wait_for_timeout(1200)
    pg.select_option("#psort", "price_asc"); pg.wait_for_timeout(1500)
    v = froms(); print("gummies price_asc:", v[:8])
    check(len(v) > 1 and ordered(v, 1), f"price low to high is non-decreasing by the shown lowest, unpriced last ({len(v)} cards)")
    pg.select_option("#psort", "price_desc"); pg.wait_for_timeout(1500)
    v = froms(); print("gummies price_desc:", v[:8])
    check(len(v) > 1 and ordered(v, -1), f"price high to low is non-increasing by the shown lowest, unpriced last ({len(v)} cards)")
    pg.screenshot(path=f"{SHOTS}/ux-price-desc.png")

    pg.fill("#q", "pre-roll"); pg.select_option("#psort", "price_asc"); pg.wait_for_timeout(1500)
    prods = api({"q": "pre-roll", "sort": "price_asc"})
    texts = pg.eval_on_selector_all("#plist .p", "els => els.map(e => e.innerText)")
    check(len(texts) == len(prods), f"sorted 'pre-roll' shows the API's first page ({len(texts)} cards)")
    pg.screenshot(path=f"{SHOTS}/ux-price-asc.png")
    pg.fill("#q", "")
    # (a) each card's "lowest $X at <store>" is /api/search's lowest / lowest_store
    for prov in ("Saskatchewan", "Alberta"):
        pg.select_option("#prov", prov); pg.select_option("#psort", ""); pg.wait_for_timeout(1500)
        prods = api({}, prov)
        texts = pg.eval_on_selector_all("#plist .p", "els => els.map(e => e.innerText)")
        bad = []
        for t, x in zip(texts, prods):
            c = card_lowest(t) if x["lowest"] is not None else None
            want = (round(x["lowest"], 2), (x["lowest_store"] or "").strip()) if x["lowest"] is not None else None
            if (c and (c[0], c[1]) != want) or (want and not c) or (not want and "lowest $" in t):
                bad.append((t.split("\n")[-1], want))
        n_priced = sum(x["lowest"] is not None for x in prods)
        check(len(texts) == len(prods) and n_priced > 0 and not bad,
              f"{prov}: every card shows 'lowest $X at <store>' matching the API ({n_priced} of {len(prods)} priced) {bad[:2]}")
    # (b) product page, whole province: the named store is a row with that bold price
    pg.select_option("#top", "all")
    card0 = pg.query_selector("#plist .p"); t0 = card0.inner_text(); c0 = card_lowest(t0)
    card0.click(); pg.wait_for_timeout(2000)
    rows = pg.eval_on_selector_all("#out .r", "els => els.map(e => [e.querySelector('.who b').innerText, e.querySelector('.pr b') ? e.querySelector('.pr b').innerText : null])")
    check(c0 is not None and any(n == c0[1] and pr == f"${c0[0]:.2f}" for n, pr in rows),
          f"product page (whole province) lists {c0 and c0[1]} at bold ${c0 and c0[0]} as on the card ({len(rows)} rows)")
    pg.click("#back"); pg.wait_for_timeout(1000)
    pg.select_option("#top", "10")
    pg.select_option("#prov", "Saskatchewan"); pg.select_option("#psort", "price_asc"); pg.wait_for_timeout(1500)
    pg.reload(); pg.wait_for_timeout(2500)
    check(pg.eval_on_selector("#psort", "e => e.value") == "price_asc", "price order remembered after reload")
    pg.select_option("#psort", ""); pg.wait_for_timeout(800)

    # failed-store notice, with the Saskatchewan file edited in flight
    def failed(route):
        resp = route.fetch(); d = resp.json()
        d["run"]["failed"] = 2; d["run"]["failed_stores"] = ["Tisdale", "Swift Current"]
        route.fulfill(response=resp, body=json.dumps(d))
    pg2 = ctx.new_page(); pg2.on("pageerror", lambda e: errs.append(str(e)))
    pg2.route("**/data/saskatchewan.json", failed)
    pg2.goto(URL); pg2.wait_for_timeout(2500)
    note = pg2.inner_text("#notice"); print("notice:", note)
    check(pg2.is_visible("#notice") and "Tisdale, Swift Current" in note, "failed-store notice names the stores")
    pg2.screenshot(path=f"{SHOTS}/ux-notice.png")

    m = b.new_page(viewport={"width": 390, "height": 844})
    m.route("https://cdn.shopify.com/**", lambda r: r.abort())
    m.goto(URL); m.wait_for_timeout(2500); m.fill("#q", "gummies"); m.wait_for_timeout(1500)
    over = m.evaluate("document.documentElement.scrollWidth - innerWidth")
    check(over <= 0, f"no sideways scroll at phone width ({over}px)")
    m.screenshot(path=f"{SHOTS}/ux-phone.png")
    check(not errs, f"no page errors {errs}")
    b.close()
srv.shutdown()
print(f"{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
