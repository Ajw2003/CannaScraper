"""Step 5 — discover how the site stores the *selected store*.

The age gate and store selection are localStorage-driven, but the exact key
holding the chosen store must be confirmed against the live site. This script
finds it two ways:

  python discover.py            # automated probe (headless)
  python discover.py --manual   # opens a window; you pick a store by hand,
                                # press Enter, and it diffs the state

Whatever it reports goes into config.py so the rest of the code reads one dict.
"""

from __future__ import annotations

import asyncio
import json
import sys

from playwright.async_api import async_playwright

import config

DUMP = "() => Object.fromEntries(Object.entries(localStorage))"


async def snapshot(page, ctx) -> tuple[dict, dict]:
    ls = await page.evaluate(DUMP)
    cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
    return ls, cookies


def diff(before: dict, after: dict) -> dict:
    out = {}
    for k, v in after.items():
        if k not in before:
            out[k] = ("ADDED", None, v)
        elif before[k] != v:
            out[k] = ("CHANGED", before[k], v)
    for k in before:
        if k not in after:
            out[k] = ("REMOVED", before[k], None)
    return out


def show(title: str, d: dict) -> None:
    print(f"\n--- {title} ---")
    if not d:
        print("  (no change)")
        return
    for k, (kind, old, new) in sorted(d.items()):
        old_s = "" if old is None else f" {str(old)[:60]!r} ->"
        print(f"  {kind:<8} {k}{old_s} {str(new)[:80]!r}")


async def main(manual: bool) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not manual)
        ctx = await browser.new_context(
            user_agent=config.USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-CA",
            timezone_id="America/Edmonton",
        )
        page = await ctx.new_page()
        page.set_default_timeout(config.NAV_TIMEOUT_MS)

        print(f"Navigating to {config.BASE} ...")
        await page.goto(config.BASE, wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)

        ls0, ck0 = await snapshot(page, ctx)
        print(f"\nlocalStorage keys after first load ({len(ls0)}):")
        for k, v in sorted(ls0.items()):
            print(f"  {k:<34} = {str(v)[:70]!r}")
        print(f"\ncookies ({len(ck0)}): {sorted(ck0)}")

        # Anything that smells like a store reference.
        print("\nStore-ish keys:")
        hits = {k: v for k, v in ls0.items()
                if any(t in k.lower() for t in ("store", "location", "shop", "province"))}
        for k, v in sorted(hits.items()) or []:
            print(f"  {k:<34} = {str(v)[:70]!r}")
        if not hits:
            print("  (none yet — likely set only after you choose a store)")

        if manual:
            print("\n" + "=" * 70)
            print("A browser window is open.")
            print(" 1. Clear the age gate.")
            print(" 2. Use 'Change Your Store' and pick a SPECIFIC store.")
            print(" 3. Come back here and press Enter.")
            print("=" * 70)
            await asyncio.get_event_loop().run_in_executor(None, input)

            ls1, ck1 = await snapshot(page, ctx)
            show("localStorage diff", diff(ls0, ls1))
            show("cookie diff", diff(ck0, ck1))

            with open("discovery.json", "w", encoding="utf-8") as f:
                json.dump({"localStorage_before": ls0, "localStorage_after": ls1,
                           "cookies_before": ck0, "cookies_after": ck1}, f, indent=1)
            print("\nSaved full state to discovery.json")
            print("Copy the key holding the store into config.py -> STORE_ID_KEYS.")
        else:
            # Automated probe: seed the age gate, reload, see what appears.
            seed = dict(config.AGE_GATE_STATE)
            seed[config.PROVINCE_KEY] = config.PROVINCE
            await page.evaluate(
                "(kv) => { for (const [k, v] of Object.entries(kv)) localStorage.setItem(k, v); }",
                seed,
            )
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(6000)
            ls1, ck1 = await snapshot(page, ctx)
            show("localStorage diff after seeding age gate", diff(ls0, ls1))
            show("cookie diff", diff(ck0, ck1))

            body = (await page.inner_text("body"))[:600]
            print("\n--- visible text (first 600 chars) ---")
            print(body)
            print("\nRe-run with --manual to capture a real store selection.")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main("--manual" in sys.argv))
