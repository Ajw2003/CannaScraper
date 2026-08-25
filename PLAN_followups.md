# Follow-up work

Deferred items, each with the evidence that found it. Written 2026-08-25 while
normalizing the `observations` table; none of these are part of that change.

Ordered by how wrong the current behaviour is, not by effort.

---

## 1. ~~`api_fetcher._call()` treats a normal response as a hard failure~~ FIXED 2026-08-25

Fixed, along with a second bug it was hiding. Verify with:

```
python verify_scan_fix.py
```

**What was actually wrong — two coupled bugs, not one.** The second masked the
first: both produced noise on uncarried products, so neither looked like the
other's cause.

**Bug 1** — success was keyed on `body["success"]`, which is false for any
batch containing an uncarried SKU. Now keyed on the presence of a `data`
object, i.e. "the server answered". Proven against the live endpoint:

```
server says success = False | message = Bag Changed
OLD  body.get("success")                -> False   => raise, 3 retries burned
NEW  isinstance(body.get("data"), dict) -> True    => accept and read it
```

**Bug 2** — `missingItems` holds VARIANT IDS while `scanned-items` is keyed by
SKU. `_row()` tested `sku not in missing` against that set, so the test could
never pass and every legitimately not-carried product was also tagged with the
error `"sku absent from response"`. `fetch()` now translates back through the
variant list:

```
missingItems (variant ids): [44094929305788, 44094930518204]
requested skus            : ['291178', '290961', '114263']
translated back to skus    : ['291178', '290961']
```

With the translation correct, that per-row test becomes the real coverage
check: a SKU in neither `scanned-items` nor `missingItems` is now the only
thing that raises the error, which is what it was always meant to mean.

Original report follows.

---

**Where:** [fetchers/api_fetcher.py:106](fetchers/api_fetcher.py)

```python
if status == 200 and isinstance(body, dict) and body.get("success"):
    return body
```

`scan-multiple-items` returns `success: false` with `message: "Bag Changed"`
whenever **any** requested SKU is not carried at that store. That is the normal
case, not an error. Observed directly:

```
POST /api/product/scan-multiple-items/3154   {"skus":[{"291178":44094929305788}]}
200 {"data":{"scanned-items":[],"elitePrices":[],
     "payload":{"291178":44094929305788},
     "missingItems":[44094929305788]},
     "message":"Bag Changed","success":false}
```

The call above is a correct, complete answer: that SKU is not stocked at 3154.
The current code falls through to the bottom of the retry loop and raises
`RuntimeError`, burning all three retries first.

**Impact:** any store where every watchlist item happens to be uncarried
produces an error row instead of three correct "not carried" rows. Masked today
only because real watchlists usually contain something the store stocks.

**Fix:** key success on transport plus coverage (`scanned-items` + `missingItems`
accounting for every SKU sent), not on `body["success"]`. Note the identifier
mismatch when doing it: `scanned-items` is keyed by **SKU**, `missingItems`
holds **variant ids**. `scan_ceiling_probe.py` already reconciles the two and
can be read as the reference.

---

## 2. `province_facts()` presents one arbitrary lot's potency as the product's

**Where:** [db.py:377](db.py) — `MAX(o.thc)`, `MAX(o.cbd)`

The docstring says "THC/CBD are product-level, so MAX() just picks a non-null
value rather than aggregating anything meaningful." Measured against the live
data, that premise is false:

| | SKUs with >1 distinct value (of 5,322) |
|---|---|
| thc | 899 |
| cbd | 793 |

SKU 112232 carries 15 distinct THC values within index runs alone — 26.01,
26.70, 26.71, 26.72, 26.78, 28.00, 28.04, 28.20, 28.29, 28.40, 28.45, 28.67,
28.70, 28.76, 29.29. These are real per-lot test results; different stores hold
different tested batches.

So `MAX()` is not "picking a non-null value", it is picking the **highest
potency any store has ever reported** and showing it as the product's potency.

**Impact:** the search UI overstates THC. Someone choosing on potency is being
shown a figure no store near them may actually have.

**Fix:** decide what the UI should mean, then say it. Options: the potency at
*this* store (per-store, requires threading store through the fact lookup), or
a range ("26.0–29.3%"), or the freshest observation. A range is probably the
honest answer for a province-wide view. Whatever is chosen, the docstring must
stop asserting the value is product-level.

This is why `thc`/`cbd` stayed on the observation row during normalization
rather than moving to `products` — collapsing them would have destroyed the
evidence this fix needs.

---

## 3. `run_id` and `scraped_at` are still repeated on every row

**Where:** [db.py](db.py) — `obs` table after normalization

Post-normalization these are the two largest remaining columns:

| column | size |
|---|---|
| `run_id` | 8.9 MB |
| `scraped_at` | 7.0 MB |

A `runs(run_id_int PK, run_id TEXT, started_at)` table with an integer FK on
`obs` would recover most of ~16 MB.

**Why it was deferred:** five queries depend on parsing the run_id string —
`run_id LIKE 'index-%'` in `index_coverage`, `last_scan_attempt`,
`scan_failure_streaks`, `indexed_store_ids`, and the `index-{Province}-{stamp}`
prefix match in `index_runs`. The run_id encodes meaning, so an integer key
changes those five call sites, not just storage. Doable, but it is a change to
the read contract and did not belong in the same step as the column split.

---

## 4. `stock_text` is derivable

**Where:** [db.py](db.py) — `obs.stock_text`, 2.3 MB

It is a function of `carried` and `available`: `"In Stock"` / `"Sold Out"` /
`"Not carried"`. Could be computed in the `observations` view like `url`
already is.

**Why it was deferred:** small win, and it is a second change to what the view
computes rather than stores. One such change at a time keeps a regression
attributable.

Check before doing it: the browser path in [scrape.py](scrape.py) may write
stock text that is not one of those three strings. If so it is not purely
derivable and this item is void.

---

## 5. `product/search` sends ~10x what we keep, and there is no way to ask for less

**Where:** [index_builder.py:67](index_builder.py) — `_get()`

Measured by [payload_probe.py](payload_probe.py): one 50-product page is
156.9 KB, of which we retain 9.3%. A full Alberta index moves ~352 MB to
extract ~33 MB.

**Status: closed, no action available.** Every sparse-fieldset convention
tried — `fields`, `_fields`, `select`, `only`, `fields[products]`, `exclude`,
`without`, `light` — returned a byte-identical response. The server also
ignores `Accept-Encoding: gzip` (160,688 bytes either way, no
`Content-Encoding` header).

Recorded so nobody re-investigates it. Re-run `payload_probe.py` if the API
ever changes; it will detect a working parameter automatically.

---

## 6. `scan-multiple-items` cannot replace the province index

**Status: closed, investigated and rejected.** Kept here so the reasoning is
not lost and the idea is not re-proposed.

Measured by [scan_ceiling_probe.py](scan_ceiling_probe.py) against store 3154:

| SKUs sent | seconds | result |
|---|---|---|
| 1 | 2.3 | ok |
| 25 | 7.9 | ok |
| 100 | 37.3 | ok |
| 250 | 77.9 | ok |
| 500 | 150.7 | ok |
| 1000 | 172.9 | HTTP 500 |

Ceiling is between 500 and 1000 SKUs per call, and latency is **linear at
~0.3 s per SKU** — batching moves the same work into fewer requests without
making it cheaper.

Cost of a full Alberta refresh:

| path | per-product | total |
|---|---|---|
| `product/search` | 1.2 s ÷ 50 = 0.024 s | ~2,300 calls, ~1 h |
| `scan`, full catalogue | 0.3 s × 6,699 × 92 | ~51 h |
| `scan`, last-known in-stock only (~1,200/store) | 0.3 s × 1,200 × 92 | ~9 h |

Even the optimistic row is ~9 h sequential (~1.5 h at `API_CONCURRENCY = 6`),
worse than the hour already achieved, and it regresses correctness: anything
that came *into* stock since the last run is not in the query set and stays
invisible.

`product/search` is ~12x more time-efficient per product. The endpoint that
sends more data is the cheaper one, because its cost is bandwidth while scan's
is server compute.

`scan-multiple-items` remains correct for what it already does — the live check
of a short, known watchlist across many stores, where cost scales with stores
rather than products. No change needed there.
