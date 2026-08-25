"""A durable, multi-worker work queue for index runs -- one item per store.

Why not Redis / RabbitMQ / Celery
---------------------------------
Those are the textbook answer to "decouple the workers with a message broker",
and they are the wrong answer *here*. This app ships as a PyInstaller
executable that a non-technical user unzips and double-clicks; it keeps its
data under %LOCALAPPDATA% precisely so it can be replaced by copying a folder.
Requiring a broker daemon to be installed, running, and reachable before a
stock index will build would trade a working desktop tool for an ops problem.

The properties a broker was wanted for are these:

    durable        work survives the process dying mid-run
    atomic claim   N workers, and no store ever fetched twice
    redelivery     a worker that dies mid-item does not strand it
    idempotent     re-running an item is harmless

All four come out of the SQLite database this app already opens -- already in
WAL mode, already carrying a busy timeout, already the thing an index run
writes its results to. At one queue, one producer, and a handful of workers on
one machine, that is not a compromise; it is the same guarantees without a
second moving part. The day this needs to fan out across machines is the day
the broker earns its keep, and `claim()` is the only function that would change.

`in_process` state lives nowhere: everything is a row, so `--resume` and a
crash-restart are the same code path.

Claim protocol
--------------
No `BEGIN IMMEDIATE`, no `RETURNING`. Claiming is a conditional UPDATE whose
WHERE clause re-checks the state, and the winner is whoever gets `rowcount ==
1`. SQLite serializes writers, so exactly one worker can win a given row --
losers simply try the next candidate. That works on every SQLite the packaged
build might carry and needs no transaction handling of its own.

A claim carries a lease. If a worker dies holding one, the row becomes
claimable again after `WORK_LEASE_S` and another worker picks it up. Redelivery
is safe because `db.write_rows()` is `INSERT OR REPLACE` keyed on
`(run_id, store_id, sku)` -- a store indexed twice overwrites itself.
"""

from __future__ import annotations

import sqlite3
import time

import config

PENDING = "pending"
CLAIMED = "claimed"
DONE = "done"
FAILED = "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_queue (
  run_id     TEXT    NOT NULL,
  store_id   TEXT    NOT NULL,
  province   TEXT,
  store_name TEXT,
  city       TEXT,
  seq        INTEGER NOT NULL DEFAULT 0,  -- original store order, for fairness
  state      TEXT    NOT NULL DEFAULT 'pending',
  attempts   INTEGER NOT NULL DEFAULT 0,
  worker     TEXT,
  claimed_at REAL,
  finished_at REAL,
  error      TEXT,
  PRIMARY KEY (run_id, store_id)
);

CREATE INDEX IF NOT EXISTS idx_wq_claim ON work_queue(run_id, state, seq);
"""


def ensure(conn: sqlite3.Connection) -> None:
    """Create the queue table. Safe to call on every connection."""
    conn.executescript(SCHEMA)
    conn.commit()


def enqueue(conn: sqlite3.Connection, run_id: str, province: str,
            stores: list[dict]) -> int:
    """Add stores to a run's queue. Returns how many were newly added.

    INSERT OR IGNORE, so enqueueing the same run twice -- which is exactly
    what --resume does -- leaves already-finished items alone rather than
    resetting them to pending.
    """
    ensure(conn)
    rows = [(run_id, str(s["store_id"]), province, s.get("name", ""),
             s.get("city", ""), i)
            for i, s in enumerate(stores)]
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO work_queue "
        "(run_id, store_id, province, store_name, city, seq) "
        "VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    return conn.total_changes - before


def mark_done_from_history(conn: sqlite3.Connection, run_id: str,
                           done_ids: set[str]) -> int:
    """Reconcile the queue with observations already written for this run.

    A run started before this queue existed -- or resumed from a database that
    has its rows but not its queue -- must not re-fetch stores whose data is
    already sitting in `obs`. `db.done_store_ids()` is the authority on what
    actually landed; this folds that back into the queue.
    """
    if not done_ids:
        return 0
    marks = ",".join("?" * len(done_ids))
    cur = conn.execute(
        f"UPDATE work_queue SET state='{DONE}', finished_at=? "
        f"WHERE run_id=? AND state<>'{DONE}' AND store_id IN ({marks})",
        [time.time(), run_id] + [str(s) for s in done_ids])
    conn.commit()
    return cur.rowcount


def claim(conn: sqlite3.Connection, run_id: str, worker: str) -> dict | None:
    """Take the next available item, or None if the run is drained.

    Available means pending, or claimed by someone whose lease has expired.
    Retryable failures come back as pending too (see `fail`), so they queue
    behind everything untried rather than spinning at the head of the line.
    """
    now = time.time()
    stale = now - config.WORK_LEASE_S
    for _ in range(config.WORK_CLAIM_TRIES):
        cur = conn.execute(
            f"""
            SELECT store_id, store_name, city, province, attempts, seq
            FROM work_queue
            WHERE run_id=? AND (state='{PENDING}'
                                OR (state='{CLAIMED}' AND claimed_at < ?))
            ORDER BY state='{CLAIMED}', seq
            LIMIT ?
            """,
            (run_id, stale, config.WORK_CLAIM_TRIES))
        candidates = cur.fetchall()
        if not candidates:
            return None

        for sid, name, city, prov, attempts, seq in candidates:
            hit = conn.execute(
                f"""
                UPDATE work_queue
                SET state='{CLAIMED}', worker=?, claimed_at=?, attempts=attempts+1
                WHERE run_id=? AND store_id=?
                  AND (state='{PENDING}'
                       OR (state='{CLAIMED}' AND claimed_at < ?))
                """,
                (worker, now, run_id, sid, stale))
            conn.commit()
            if hit.rowcount == 1:
                return {"store_id": sid, "name": name or "", "city": city or "",
                        "province": prov or "", "attempts": attempts + 1,
                        "seq": seq}
            # Lost the race to another worker; try the next candidate.
    return None


def complete(conn: sqlite3.Connection, run_id: str, store_id: str) -> None:
    conn.execute(
        f"UPDATE work_queue SET state='{DONE}', finished_at=?, error=NULL "
        f"WHERE run_id=? AND store_id=?", (time.time(), run_id, str(store_id)))
    conn.commit()


def fail(conn: sqlite3.Connection, run_id: str, store_id: str,
         error: str) -> bool:
    """Record a failed attempt. Returns True if it will be retried.

    A store gets `WORK_MAX_ATTEMPTS` tries before it is given up on, so one
    transient 500 does not cost the store its place in the index while a store
    that is genuinely broken does not consume the whole run retrying.
    """
    row = conn.execute(
        "SELECT attempts FROM work_queue WHERE run_id=? AND store_id=?",
        (run_id, str(store_id))).fetchone()
    attempts = row[0] if row else config.WORK_MAX_ATTEMPTS
    retry = attempts < config.WORK_MAX_ATTEMPTS
    conn.execute(
        f"UPDATE work_queue SET state=?, error=?, finished_at=? "
        f"WHERE run_id=? AND store_id=?",
        (PENDING if retry else FAILED, error[:300],
         None if retry else time.time(), run_id, str(store_id)))
    conn.commit()
    return retry


def release(conn: sqlite3.Connection, run_id: str, store_id: str) -> None:
    """Hand an item back untouched -- used when a run is cancelled mid-item.

    The attempt counter is rolled back too: stopping on purpose is not the
    store's fault, and letting a cancel burn a retry means three cancels can
    permanently fail a perfectly good store.
    """
    conn.execute(
        f"UPDATE work_queue SET state='{PENDING}', worker=NULL, "
        f"claimed_at=NULL, attempts=MAX(0, attempts-1) "
        f"WHERE run_id=? AND store_id=?", (run_id, str(store_id)))
    conn.commit()


def requeue_failed(conn: sqlite3.Connection, run_id: str) -> int:
    """Give up-on stores another chance, with a fresh attempt budget.

    This is what --resume means for a store that exhausted WORK_MAX_ATTEMPTS:
    the run is being restarted deliberately, usually after whatever broke has
    been looked at, so carrying the old attempt count forward would fail it
    again on the first hiccup. Stores that already succeeded are untouched.
    """
    cur = conn.execute(
        f"UPDATE work_queue SET state='{PENDING}', attempts=0, worker=NULL, "
        f"claimed_at=NULL, finished_at=NULL WHERE run_id=? AND state='{FAILED}'",
        (run_id,))
    conn.commit()
    return cur.rowcount


def counts(conn: sqlite3.Connection, run_id: str) -> dict:
    """How the run stands: {pending, claimed, done, failed, total}."""
    ensure(conn)
    out = {PENDING: 0, CLAIMED: 0, DONE: 0, FAILED: 0}
    for state, n in conn.execute(
            "SELECT state, COUNT(*) FROM work_queue WHERE run_id=? "
            "GROUP BY state", (run_id,)):
        out[state] = n
    out["total"] = sum(out.values())
    return out


def failures(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    """Stores this run gave up on, with the last error each reported."""
    cur = conn.execute(
        f"SELECT store_id, store_name, city, attempts, error "
        f"FROM work_queue WHERE run_id=? AND state='{FAILED}' ORDER BY seq",
        (run_id,))
    return [{"store_id": r[0], "name": r[1] or "", "city": r[2] or "",
             "attempts": r[3], "error": r[4] or ""} for r in cur.fetchall()]


def reset_stuck(conn: sqlite3.Connection, run_id: str) -> int:
    """Return every claimed item to pending. For restarting a crashed run now.

    Without this, resuming a run whose worker was killed means waiting out
    WORK_LEASE_S before those stores become claimable -- correct, but a
    pointless five minutes when we already know the previous process is gone.
    """
    cur = conn.execute(
        f"UPDATE work_queue SET state='{PENDING}', worker=NULL, claimed_at=NULL "
        f"WHERE run_id=? AND state='{CLAIMED}'", (run_id,))
    conn.commit()
    return cur.rowcount


def prune(conn: sqlite3.Connection, keep_runs: int = 20) -> int:
    """Drop queue rows for all but the most recent runs.

    The observations they produced are the durable record; the queue is
    scaffolding, and 92 rows per run adds up over a year of daily indexes.
    """
    ensure(conn)
    runs = [r[0] for r in conn.execute(
        "SELECT DISTINCT run_id FROM work_queue ORDER BY run_id DESC")]
    old = runs[keep_runs:]
    if not old:
        return 0
    marks = ",".join("?" * len(old))
    cur = conn.execute(
        f"DELETE FROM work_queue WHERE run_id IN ({marks})", old)
    conn.commit()
    return cur.rowcount
