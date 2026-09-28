"""Browser test of the additions made on top of the original page on the Pages site.

Serves ROOT (a folder holding CannaScraper/ with site/ files and data/ from the
gh-pages branch) and walks the page in Chromium: card count and price, data
age line, browse with an empty box, "view" links, Back to search, remembered
province, the failed-stores notice (Saskatchewan's file edited in flight), and
no sideways scroll at phone width. Screenshots go to SHOTS.

    python3 ci/pages_ux_check.py ROOT SHOTS

See docs/plans/restore-original-page.md, "Additions from the first static site".
"""
import functools, http.server, json, threading, sys
from playwright.sync_api import sync_playwright

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
    b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    ctx = b.new_context(viewport={"width": 1100, "height": 900})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.route("https://cdn.shopify.com/**", lambda r: r.abort())
    pg.goto(URL); pg.wait_for_timeout(2500)

    age = pg.inner_text("#age"); print("age line:", age)
    check("Alberta stock updated" in age, "age line names the default province")
    n = len(pg.query_selector_all("#plist .p"))
    check(n == 50, f"empty box browses: {n} cards on load")
    first = pg.inner_text("#plist .p >> nth=0").replace("\n", " | "); print("first card:", first)
    check(" stores" in first or " store " in first, "card shows store count")
    check("from $" in first, "card shows 'from $' price")
    print("more:", pg.inner_text("#more").replace("\n", " | "))
    pg.screenshot(path=f"{SHOTS}/ux-landing.png")

    pg.select_option("#prov", "Saskatchewan"); pg.wait_for_timeout(1500)
    check("Saskatchewan stock updated" in pg.inner_text("#age"), "age line follows province change")
    pg.fill("#q", "blue dream"); pg.wait_for_timeout(1200)
    cards = pg.query_selector_all("#plist .p"); print("blue dream cards:", len(cards))
    check(len(cards) == 19, "search 'blue dream' in Saskatchewan still returns 19")
    pg.screenshot(path=f"{SHOTS}/ux-search.png")

    pg.fill("#q", ""); pg.wait_for_timeout(1200)
    pg.click("#moreb"); pg.wait_for_timeout(1200)
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
    check(n2 == 100, f"back restores the list at the same length ({n2})")
    vis = pg.evaluate(f"""(() => {{ const r = document.querySelector('#plist .p[data-sku="{sku}"]').getBoundingClientRect();
                         return r.top > 0 && r.bottom < innerHeight; }})()""")
    check(vis, "back scrolls to the card that was opened")

    pg.reload(); pg.wait_for_timeout(2500)
    check(pg.eval_on_selector("#prov", "e => e.value") == "Saskatchewan", "province remembered after reload")

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
