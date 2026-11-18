import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from uuid import uuid4

from .contract import Issue


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        cfg.db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(cfg.db, os.O_CREAT | os.O_RDWR, 0o600))
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, request_id TEXT UNIQUE, fingerprint TEXT NOT NULL,
                target TEXT NOT NULL, request_json TEXT NOT NULL, request_sha256 TEXT NOT NULL, format TEXT NOT NULL, dispatch_at REAL,
                created REAL NOT NULL, updated REAL NOT NULL, status TEXT NOT NULL,
                run_id INTEGER, attempt INTEGER, run_url TEXT, html_url TEXT, error TEXT,
                manifest TEXT, next_check REAL NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0)""")

            db.execute(
                "CREATE TABLE IF NOT EXISTS dispatch_hold (target TEXT PRIMARY KEY, until REAL NOT NULL)"
            )

    def pause_dispatch(self, delay):
        with self.connection() as db:
            db.execute(
                """INSERT INTO dispatch_hold VALUES (?,?) ON CONFLICT(target) DO UPDATE
                SET until=MAX(until, excluded.until)""",
                (self.cfg.target, time.time() + delay),
            )

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.cfg.db, timeout=1)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, job_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise Issue("unknown_job")
            return dict(row)

    def reserve(self, req):
        payload = req.payload_json()
        digest = hashlib.sha256(payload.encode()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps([self.cfg.target, digest]).encode()).hexdigest()
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if req.request_id:
                row = db.execute("SELECT * FROM jobs WHERE request_id=?", (req.request_id,)).fetchone()
                if row:
                    if row["fingerprint"] != fingerprint:
                        raise Issue("idempotency_conflict")
                    return dict(row), False
            hold = db.execute(
                "SELECT until FROM dispatch_hold WHERE target=?", (self.cfg.target,)
            ).fetchone()
            if hold and hold[0] > now:
                raise Issue("rate_limited", int(hold[0] - now) + 1)
            active = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('staging','submitting','queued','running')"
            ).fetchone()[0]
            if active >= self.cfg.active:
                raise Issue("busy")
            if (
                db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE created>=?", (now // 86400 * 86400,)
                ).fetchone()[0]
                >= self.cfg.daily
            ):
                raise Issue("daily_limit")
            job = str(uuid4())
            db.execute(
                """INSERT INTO jobs(job_id,request_id,fingerprint,target,request_json,request_sha256,format,created,updated,status)
                   VALUES (?,?,?,?,?,?,?,?,?,'staging')""",
                (
                    job,
                    req.request_id,
                    fingerprint,
                    self.cfg.target,
                    payload,
                    digest,
                    req.format,
                    now,
                    now,
                ),
            )
        return self.get(job), True

    def claim_dispatch(self, job_id):
        """Only the caller that transitions staging -> submitting may send the POST."""
        with self.connection() as db:
            now = time.time()
            cursor = db.execute(
                """UPDATE jobs SET status='submitting', dispatch_at=?, updated=?, version=version+1,
                next_check=0, error=NULL WHERE job_id=? AND status='staging'""",
                (now, now, job_id),
            )
            return cursor.rowcount == 1

    def active_jobs(self):
        with self.connection() as db:
            return [
                r[0]
                for r in db.execute(
                    """SELECT job_id FROM jobs WHERE target=? AND
                status IN ('staging','submitting','queued','running') ORDER BY created LIMIT 5""",
                    (self.cfg.target,),
                )
            ]

    def update(self, row, **changes):
        allowed = {
            "status",
            "run_id",
            "attempt",
            "error",
            "manifest",
            "next_check",
            "run_url",
            "html_url",
        }
        assert changes and changes.keys() <= allowed
        with self.connection() as db:
            fields = ", ".join(f"{k}=?" for k in changes)
            db.execute(
                f"UPDATE jobs SET {fields}, updated=?, version=version+1 WHERE job_id=? AND version=?",
                (*changes.values(), time.time(), row["job_id"], row["version"]),
            )
        return self.get(row["job_id"])
