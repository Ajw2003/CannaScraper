"""Internal-API backend -- deliberately unimplemented.

The site prices every product by calling

    POST app.cannacabana.com/api/product/scan-single-item/<store_id>
         {"skus": [{"<sku>": <variant_id>}]}

and a `scan-multiple-items` variant accepts a whole watchlist in one call. It
returns the same positional CSV we already decode in scrape.py
(`[0]` stock, `[1]` member price, `[2]` retail price, `[6]` gram equivalence),
plus ELITE pricing.

Using it would take a 92-store sweep from ~18 minutes to a couple of seconds.

It is NOT implemented because it requires the client_id/client_secret embedded
in the site's page source. Those are public in the sense that every visitor's
browser receives them, and no employee or store account is involved -- but they
are not published for third-party use, and can be rotated without notice.

That is a call for the project owner, not a default. To adopt it:

  1. Decide the terms-of-service question deliberately.
  2. Implement `fetch()` below, mirroring the row shape that
     `scrape.scrape_variant()` produces so nothing downstream changes.
  3. Set config.FETCHER = "api".
  4. Diff a 10-store run against the browser backend before trusting it --
     they should agree on price, stock, and carried for every row.
"""

from __future__ import annotations


class ApiFetcher:
    name = "api"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def fetch(self, store: dict, variants: list[dict],
                    verbose: bool = True) -> list[dict]:
        raise NotImplementedError(
            "The API fetcher is a deliberate stub -- see the module docstring. "
            "Set config.FETCHER = 'browser' to use the working backend."
        )
