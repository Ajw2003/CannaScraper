"""One worker thread, one queue, for everything that talks to the site.

Why serialized: `config.API_RATE_PER_MIN = 50` is a single global budget
against a single upstream host. Two jobs at once produce 429s, not throughput.
And a 45-minute index build cannot run on the event loop, or search stops
answering for 45 minutes.

So both job kinds go through here:

  index   a province-wide catalogue rebuild (synchronous, ~29s per store)
  live    a per-SKU re-check (asynchronous; the worker gives it its own loop)

Progress is echoed to the console as it happens. The app runs in the
foreground precisely so that a 45-minute job is something you can watch rather
than something you are told about afterwards.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
import uuid

#: Finished jobs kept for the UI. The dict this replaced never pruned and grew
#: for the life of the process.
MAX_HISTORY = 50

_lock = threading.RLock()
_jobs: dict[str, dict] = {}
_queue: queue.Queue = queue.Queue()
_worker: threading.Thread | None = None

#: Set by the server to drop its cached province facts when an index lands, so
#: new data shows immediately instead of after the 120s TTL.
index_finished_hook = None

#: Where progress lines go. The launcher points this at the console.
echo = print

ACTIVE = ("queued", "running")


class Busy(Exception):
    """Something is already queued or running for this province."""


# --- reading ---------------------------------------------------------------

def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def all_jobs() -> list[dict]:
    with _lock:
        return [dict(j) for j in
                sorted(_jobs.values(), key=lambda j: j["queued_at"], reverse=True)]


def active() -> list[dict]:
    return [j for j in all_jobs() if j["state"] in ACTIVE]


def active_for_province(province: str) -> dict | None:
    for j in all_jobs():
        if j["kind"] == "index" and j["province"] == province and j["state"] in ACTIVE:
            return j
    return None


def cancel(job_id: str) -> bool:
    """Ask a job to stop. An index stops at the next store boundary."""
    with _lock:
        job = _jobs.get(job_id)
        if not job or job["state"] not in ACTIVE:
            return False
        job["cancel_requested"] = True
        job["current"] = job["store"] = "stopping after this store..."
        return True


# --- submitting ------------------------------------------------------------

def _new(kind: str, label: str, total: int, **meta) -> dict:
    job = {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "label": label,
        "state": "queued",
        "total": total,
        "done": 0,
        "current": "",
        "eta_min": None,
        "rows": 0,
        "failed_stores": 0,
        "run_id": None,
        "province": None,
        "sku": None,
        "error": None,
        "queued_at": time.time(),
        "started": None,
        "finished_at": None,
        "cancel_requested": False,
        # Legacy keys the existing refresh-polling JS reads directly.
        "store": "",
        "finished": False,
    }
    job.update(meta)
    with _lock:
        _jobs[job["id"]] = job
    return job


def submit_index(province: str, *, resume: str | None = None,
                 limit: int | None = None) -> dict:
    """Queue a province rebuild. Raises Busy if one is already pending."""
    existing = active_for_province(province)
    if existing:
        raise Busy(f"{province} is already {existing['state']}")

    job = _new("index", f"Index {province}", 0, province=province)

    def run(job):
        _index_body(job, province, resume, limit)

    _enqueue(job, run)
    return job


def submit_live(label: str, total: int, coro_factory, **meta) -> dict:
    """Queue an async job. `coro_factory(job)` returns the coroutine to run.

    The factory indirection keeps this module free of any knowledge of
    fetchers or the database, so there is no import cycle with server.py.
    """
    job = _new("live", label, total, **meta)

    def run(job):
        asyncio.run(coro_factory(job))

    _enqueue(job, run)
    return job


def _enqueue(job: dict, fn) -> None:
    start()
    _queue.put((job["id"], fn))
    depth = _queue.qsize()
    if depth > 1:
        echo(f"  [job] queued {job['label']} (#{depth} in line)")


# --- the work itself -------------------------------------------------------

def _index_body(job: dict, province: str, resume: str | None,
                limit: int | None) -> None:
    import index_builder

    def progress(ev: dict) -> None:
        phase = ev["phase"]
        with _lock:
            job["run_id"] = ev["run_id"]
            job["total"] = ev["total"]
            job["done"] = ev["done"]
            if ev["eta_min"] is not None:
                job["eta_min"] = ev["eta_min"]
            if ev.get("store"):
                where = ev["store"]
                if ev.get("city"):
                    where = f"{where}, {ev['city']}"
                job["current"] = job["store"] = where
            if phase == "stored":
                job["rows"] += ev["rows"]
            elif phase == "failed":
                job["failed_stores"] += 1
                job["error"] = f"{ev['store']}: {ev['error']}"

        if phase == "begin":
            echo(f"  [index] {province}: {ev['total']} stores, "
                 f"~{ev['eta_min']:.0f} min   run {ev['run_id']}")
        elif phase == "stored":
            eta = ev["eta_min"]
            eta_s = f"   ETA {eta:.0f}m" if eta is not None else ""
            echo(f"  [index] [{ev['done']}/{ev['total']}] {ev['store'][:26]:<26} "
                 f"{ev['rows']:>5} products, {ev['instock']:>5} in stock{eta_s}")
        elif phase == "failed":
            echo(f"  [index] [{ev['done']}/{ev['total']}] {ev['store'][:26]:<26} "
                 f"FAILED {ev['error']}")

    res = index_builder.build_index(
        province, limit=limit, resume=resume, on_progress=progress,
        should_stop=lambda: job["cancel_requested"])

    with _lock:
        job["result"] = res
        job["run_id"] = res["run_id"]
        if res["cancelled"]:
            job["state"] = "cancelled"

    verb = "cancelled after" if res["cancelled"] else "done:"
    tail = f", {res['failed']} failed" if res["failed"] else ""
    echo(f"  [index] {province} {verb} {res['completed']}/{res['stores']} stores, "
         f"{res['rows']} rows, {res['minutes']:.1f} min{tail}")

    if index_finished_hook:
        try:
            index_finished_hook(province, res)
        except Exception as e:                                    # noqa: BLE001
            echo(f"  [index] post-run hook failed: {type(e).__name__}: {e}")


def _drain() -> None:
    while True:
        job_id, fn = _queue.get()
        job = _jobs.get(job_id)
        if job is None:
            _queue.task_done()
            continue
        with _lock:
            if job["cancel_requested"]:
                job["state"] = "cancelled"
            else:
                job["state"] = "running"
                job["started"] = time.time()
        try:
            if job["state"] == "running":
                fn(job)
                with _lock:
                    if job["state"] == "running":
                        job["state"] = "cancelled" if job["cancel_requested"] else "done"
        except Exception as e:                                    # noqa: BLE001
            with _lock:
                job["state"] = "error"
                job["error"] = f"{type(e).__name__}: {e}"[:300]
            echo(f"  [job] {job['label']} failed: {job['error']}")
        finally:
            with _lock:
                job["finished"] = True
                job["finished_at"] = time.time()
            _prune()
            _queue.task_done()


def _prune() -> None:
    with _lock:
        done = sorted((j for j in _jobs.values() if j["finished"]),
                      key=lambda j: j["finished_at"] or 0)
        if len(done) > MAX_HISTORY:
            for j in done[:len(done) - MAX_HISTORY]:
                _jobs.pop(j["id"], None)


def start() -> None:
    """Spin the worker up once, lazily."""
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_drain, name="job-worker",
                                       daemon=True)
            _worker.start()
