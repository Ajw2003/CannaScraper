"""Measure the rate limit instead of taking the header's word for it.

    .venv\\Scripts\\python ratelimit_probe.py                 # safe phases
    .venv\\Scripts\\python ratelimit_probe.py --find-ceiling  # adds the ramp
    .venv\\Scripts\\python ratelimit_probe.py --dry-run       # plan only

`X-RateLimit-Limit: 60` is what the server advertises. This script checks
whether that is true rather than assuming it.

First run, 2026-08-25, 297 requests:

    A  one request costs exactly one unit (drops were 1,1,1,1,1,1)
    B  fixed 60s window; the allowance snaps back to full, it does not trickle
    D  the budget is SHARED with scan-multiple-items
    C  refused at request 61 of a window, with Retry-After: 9

So the advertised figure is exact and enforced. Re-run it if their behaviour
seems to change; the CSV appends, so runs stay comparable.

Four phases, each answering one question:

    A  unit cost      does one request cost one unit?
    B  window shape   fixed window or rolling, and how long?
    D  sharing        is the budget shared with the scan endpoint?
    C  ceiling        where does it actually refuse?      (--find-ceiling only)

A, B and D stay under the advertised limit throughout. C is the only phase that
deliberately provokes a refusal, and it stops at the first one.

This is someone else's server. It is bounded by a total request budget and a
wall clock, it refuses to run alongside a job, and it stops on the first 429
rather than pushing to find the shape of the block.

Everything lands in ratelimit_probe.csv -- deliberately separate from
ratelimit.json, so an experiment never contaminates the telemetry the app
reports.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import catalog
import config
import paths
import stores as S

SEARCH = config.API_BASE + "/product/search"
CSV_PATH = paths.data("ratelimit_probe.csv")
SUMMARY_PATH = paths.data("ratelimit_probe_summary.json")

COLUMNS = ["ts_iso", "phase", "endpoint", "store_id", "target_rate", "seq",
           "status", "latency_ms", "limit", "remaining", "retry_after", "note"]

# Enough for the window to roll over between phases, so each starts from a
# full allowance rather than the previous phase's leftovers.
COOLDOWN_S = 70


class Budget(Exception):
    """Out of requests or out of time."""


class Refused(Exception):
    """A 429 arrived. Stop."""


class Probe:
    def __init__(self, args):
        self.args = args
        self.sent = 0
        self.deadline = time.time() + args.max_minutes * 60
        self.rows: list[dict] = []
        self.store = None
        self.ceiling: dict = {}
        self.sku = None
        self.variant_id = None
        self._fh = None
        self._writer = None

    # -- plumbing ----------------------------------------------------------

    def open(self):
        self._fh = open(CSV_PATH, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=COLUMNS)
        if self._fh.tell() == 0:
            self._writer.writeheader()
            self._fh.flush()

    def close(self):
        if self._fh:
            self._fh.close()

    def record(self, **row):
        row.setdefault("ts_iso", datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"))
        for col in COLUMNS:
            row.setdefault(col, "")
        self.rows.append(row)
        # Flushed per row so a Ctrl-C still leaves usable data.
        self._writer.writerow({k: row[k] for k in COLUMNS})
        self._fh.flush()

    def check_budget(self):
        if self.sent >= self.args.max_requests:
            raise Budget(f"request cap reached ({self.args.max_requests})")
        if time.time() > self.deadline:
            raise Budget(f"time cap reached ({self.args.max_minutes} min)")

    # -- the two endpoints -------------------------------------------------

    def search(self, phase: str, seq: int, target_rate="", note="") -> dict:
        self.check_budget()
        q = urllib.parse.urlencode({
            "title": "a", "storeId": self.store["store_id"], "limit": 50,
            "page": 1, "province": self.store["province"]})
        req = urllib.request.Request(
            f"{SEARCH}?{q}",
            headers={"User-Agent": config.USER_AGENT,
                     "Content-type": "application/json"})
        return self._send(req, "product/search", phase, seq, target_rate, note)

    def scan(self, phase: str, seq: int, note="") -> dict:
        self.check_budget()
        payload = json.dumps({"skus": [{str(self.sku): self.variant_id}]}).encode()
        req = urllib.request.Request(
            config.API_SCAN.format(store_id=self.store["store_id"]),
            data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": config.USER_AGENT})
        return self._send(req, "scan-multiple-items", phase, seq, "", note)

    def _send(self, req, endpoint, phase, seq, target_rate, note) -> dict:
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                status, headers = r.status, dict(r.headers)
                r.read()
        except urllib.error.HTTPError as e:
            status, headers = e.code, dict(e.headers)
            e.read()
        except Exception as exc:                                  # noqa: BLE001
            status, headers = type(exc).__name__, {}
        latency = round((time.perf_counter() - t0) * 1000)
        self.sent += 1

        def head(name):
            for k, v in headers.items():
                if k.lower() == name:
                    return v
            return ""

        row = {"phase": phase, "endpoint": endpoint, "seq": seq,
               "store_id": self.store["store_id"], "target_rate": target_rate,
               "status": status, "latency_ms": latency,
               "limit": head("x-ratelimit-limit"),
               "remaining": head("x-ratelimit-remaining"),
               "retry_after": head("retry-after"), "note": note}
        self.record(**row)
        return row

    # -- phases ------------------------------------------------------------

    def phase_a(self) -> dict:
        """Does one request cost one unit?"""
        say("\nPhase A - unit cost: 8 single requests, 12s apart")
        seen = []
        for i in range(8):
            if i:
                time.sleep(12)
            row = self.search("A", i + 1, note="isolated")
            seen.append(row)
            say(f"   {i + 1}/8  {row['status']}  remaining={row['remaining']}"
                f"  {row['latency_ms']}ms")
        nums = [int(r["remaining"]) for r in seen if str(r["remaining"]).isdigit()]
        drops = [a - b for a, b in zip(nums, nums[1:]) if a >= b]
        return {"samples": len(nums),
                "drops": drops,
                "median_drop": statistics.median(drops) if drops else None,
                "note": "delta in remaining between consecutive isolated calls"}

    def phase_b(self) -> dict:
        """Fixed window or rolling, and how long?"""
        say("\nPhase B - window shape: burst of 10, then watch it recover")
        for i in range(10):
            row = self.search("B", i + 1, note="burst")
            say(f"   burst {i + 1}/10  remaining={row['remaining']}")
        low = row["remaining"]

        say(f"   depleted to {low}; probing every 5s for 120s")
        curve = []
        for i in range(24):
            time.sleep(5)
            row = self.search("B", 100 + i, note=f"recovery+{(i + 1) * 5}s")
            curve.append((int((i + 1) * 5), row["remaining"]))
            say(f"   +{(i + 1) * 5:>3}s  remaining={row['remaining']}")
        return {"after_burst": low, "recovery": curve,
                "note": "a step back to full = fixed window; a climb = rolling"}

    def phase_d(self) -> dict:
        """Is the budget shared between the two endpoints?"""
        say("\nPhase D - endpoint sharing")
        before_scan = self.scan("D", 1, note="control before")
        before = self.search("D", 2, note="control search")
        say(f"   before: search={before['remaining']} scan={before_scan['remaining']}")
        for i in range(10):
            self.search("D", 10 + i, note="consume on search")
        after_search = self.search("D", 90, note="after consume, search")
        after_scan = self.scan("D", 91, note="after consume, scan")
        say(f"   after : search={after_search['remaining']} "
            f"scan={after_scan['remaining']}")
        return {"search_before": before["remaining"],
                "search_after": after_search["remaining"],
                "scan_before": before_scan["remaining"],
                "scan_after": after_scan["remaining"],
                "note": "if the scan figure fell too, the budget is shared"}

    def phase_c(self) -> dict:
        """Where does it actually refuse?"""
        say("\nPhase C - ceiling ramp. Stops at the first 429.")
        # Kept on the instance, not just in a local: raising Refused
        # unwinds past the return, and the clean steps leading up to
        # the refusal are the more useful half of the answer.
        result = self.ceiling = {"steps": [], "first_429": None,
                                 "max_clean_rate": None}
        for rate in (30, 40, 50, 60, 70, 80):
            need = rate
            if self.sent + need > self.args.max_requests:
                say(f"   stopping before {rate}/min - not enough request budget")
                break
            say(f"   holding {rate}/min for 60s ({need} requests)")
            interval = 60.0 / rate
            lowest, refused = None, None
            start = time.time()
            for i in range(need):
                target = start + i * interval
                gap = target - time.time()
                if gap > 0:
                    time.sleep(gap)
                row = self.search("C", i + 1, target_rate=rate, note="ramp")
                rem = row["remaining"]
                if str(rem).isdigit():
                    rem = int(rem)
                    lowest = rem if lowest is None else min(lowest, rem)
                if row["status"] == 429:
                    refused = row
                    break
            step = {"rate": rate, "sent": i + 1, "lowest_remaining": lowest,
                    "refused": bool(refused)}
            result["steps"].append(step)
            say(f"     -> lowest remaining {lowest}"
                + ("   *** 429 ***" if refused else "   no refusal"))
            if refused:
                result["first_429"] = {
                    "rate": rate, "at_request": i + 1,
                    "remaining": refused["remaining"],
                    "retry_after": refused["retry_after"]}
                say(f"\n   Refused at {rate}/min after {i + 1} requests. "
                    f"Stopping, as designed.")
                raise Refused(json.dumps(result["first_429"]))
            result["max_clean_rate"] = rate
            if rate != 80:
                say(f"   cooling down {COOLDOWN_S}s before the next step")
                time.sleep(COOLDOWN_S)
        return result


def say(msg: str) -> None:
    print(msg, flush=True)


def app_busy(port: int) -> bool:
    """Refuse to measure while a job is running: it would share the budget."""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/index/status", timeout=3) as r:
            return bool(json.load(r).get("busy"))
    except Exception:                                             # noqa: BLE001
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Measure the API's real rate limit")
    ap.add_argument("--find-ceiling", action="store_true",
                    help="add the ramp that deliberately provokes a 429")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan and send nothing")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    ap.add_argument("--max-requests", type=int, default=400)
    ap.add_argument("--max-minutes", type=int, default=None)
    ap.add_argument("--store", default=None,
                    help="store id to probe against (default: first in "
                         "config.PROVINCE)")
    args = ap.parse_args(argv)
    if args.max_minutes is None:
        args.max_minutes = 25 if args.find_ceiling else 10

    phases = ["A unit cost (8 req)", "B window shape (34 req)",
              "D endpoint sharing (14 req)"]
    if args.find_ceiling:
        phases.append("C ceiling ramp (up to 330 req, stops at first 429)")

    print("=" * 74)
    print("  Rate limit probe")
    print("=" * 74)
    print(f"  target      : {config.API_BASE}")
    print(f"  phases      : {len(phases)}")
    for p in phases:
        print(f"                {p}")
    print(f"  hard caps   : {args.max_requests} requests, "
          f"{args.max_minutes} minutes")
    print(f"  records to  : {CSV_PATH}")
    if args.find_ceiling:
        print()
        print("  Phase C deliberately pushes until the server refuses once.")
        print("  It stops at that first 429 and does not probe the block.")
    print()

    if args.dry_run:
        print("  --dry-run: nothing sent.")
        return 0

    port = 8000
    try:
        import auth
        port = int(auth.load().get("port") or 8000)
    except Exception:                                             # noqa: BLE001
        pass
    if app_busy(port):
        print("  A job is running in the app on port "
              f"{port}. It shares this budget, which would both skew the")
        print("  measurement and add load. Wait for it to finish.")
        return 2

    if not args.yes:
        try:
            if input("  Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
                print("  Cancelled.")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\n  No console to confirm on; re-run with --yes.")
            return 1

    probe = Probe(args)
    stores = S.get_stores(province=args.store and "" or config.PROVINCE)
    if args.store:
        stores = [s for s in stores if str(s["store_id"]) == str(args.store)]
    if not stores:
        print("  No such store.")
        return 2
    probe.store = stores[0]

    variant = next((v for v in catalog.get_catalog(verbose=False)
                    if v.get("sku") and v.get("variant_id")), None)
    if not variant:
        print("  No usable variant in the catalogue.")
        return 2
    probe.sku, probe.variant_id = variant["sku"], variant["variant_id"]
    print(f"  probing store {probe.store['store_id']} "
          f"({probe.store['name']}, {probe.store['city']})")

    summary = {"started": datetime.now(timezone.utc).isoformat(),
               "store_id": probe.store["store_id"],
               "advertised_limit": None, "phases": {}}
    probe.open()
    started = time.time()
    try:
        summary["phases"]["A_unit_cost"] = probe.phase_a()
        say(f"\n   cooling down {COOLDOWN_S}s")
        time.sleep(COOLDOWN_S)
        summary["phases"]["B_window"] = probe.phase_b()
        say(f"\n   cooling down {COOLDOWN_S}s")
        time.sleep(COOLDOWN_S)
        summary["phases"]["D_sharing"] = probe.phase_d()
        if args.find_ceiling:
            say(f"\n   cooling down {COOLDOWN_S}s")
            time.sleep(COOLDOWN_S)
            summary["phases"]["C_ceiling"] = probe.phase_c()
    except Refused as e:
        found = getattr(probe, "ceiling", None) or {"steps": []}
        found["first_429"] = json.loads(str(e))
        summary["phases"]["C_ceiling"] = found
        summary["outcome"] = "refused - ceiling found"
    except Budget as e:
        summary["outcome"] = f"stopped early: {e}"
        say(f"\n  Stopped: {e}")
    except KeyboardInterrupt:
        summary["outcome"] = "interrupted"
        say("\n  Interrupted. Partial data is in the CSV.")
    else:
        summary["outcome"] = "completed"
    finally:
        limits = [r["limit"] for r in probe.rows if str(r["limit"]).isdigit()]
        summary["advertised_limit"] = int(limits[0]) if limits else None
        summary["requests_sent"] = probe.sent
        summary["minutes"] = round((time.time() - started) / 60, 1)
        probe.close()
        with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

    print()
    print("=" * 74)
    print(f"  {probe.sent} requests in {summary['minutes']} min. "
          f"Outcome: {summary['outcome']}")
    print(f"  per-request data : {CSV_PATH}")
    print(f"  conclusions      : {SUMMARY_PATH}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
