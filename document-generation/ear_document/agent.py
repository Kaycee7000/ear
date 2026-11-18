import asyncio
import json
import math
import random
import sqlite3
import time

from pydantic import ValidationError

from .config import Settings, credential, setting
from .contract import MESSAGES, Issue, Lookup, Submit, failure
from .github import GitHub
from .storage import B2
from .store import Store


def pending(row, delay=2, code=None):
    status = (
        row["status"] if row["status"] in ("staging", "submitting", "running", "queued") else "checking"
    )
    reply = {"status": status, "job_id": row["job_id"], "retry_after_seconds": max(1, math.ceil(delay))}
    if row["run_id"]:
        reply["workflow_run_id"] = row["run_id"]
    if code:
        reply.update(error_code=code, message=MESSAGES[code])
    return reply


def problem(code, **info):
    result = failure(code, **info)
    if code in ("busy", "daily_limit"):
        result.update(
            status="busy" if code == "busy" else "limit_reached",
            retry_after_seconds=15 if code == "busy" else 86400 - int(time.time()) % 86400,
        )
    return result


class Agent:
    def __init__(self, cfg, store, github, b2, requests):
        self.cfg, self.store, self.gh, self.b2 = cfg, store, github, b2
        self.requests = requests

    def terminal(self, row, code):
        return self.store.update(row, status="failed", error=code, manifest=None, next_check=0)

    async def start(self, row):
        # Concurrent helpers can PUT identical private request bytes. Only one wins the POST.
        if self.requests is None:
            self.requests = B2(self.cfg.region, self.cfg.bucket, *credential("REQUEST_WRITE"))
        try:
            await self.requests.stage(self.cfg.prefix, row)
        except Issue as exc:
            if exc.code == "staging_rejected":
                return self.terminal(row, exc.code)
            raise
        if not self.store.claim_dispatch(row["job_id"]):
            return self.store.get(row["job_id"])
        row = self.store.get(row["job_id"])
        try:
            mapping = await self.gh.dispatch(row)
        except Issue as exc:
            latest = self.store.get(row["job_id"])
            if latest["run_id"]:
                return latest
            if exc.code in ("dispatch_rejected", "rate_limited"):
                if exc.code == "rate_limited":
                    self.store.pause_dispatch(exc.delay)
                return self.terminal(latest, exc.code)
            return self.store.update(latest, error=exc.code, next_check=time.time() + 3)
        latest = self.store.get(row["job_id"])
        if latest["run_id"]:
            return latest
        return self.store.update(latest, status="queued", error=None, next_check=0, **mapping)

    async def observe(self, row):
        """Update one version of the job. Stale observers cannot overwrite newer attempts."""
        if row["target"] != self.cfg.target:
            raise Issue("configuration")
        if row["status"] == "staging":
            return await self.start(row)
        if not row["run_id"]:
            if row["status"] == "failed" and row["error"] != "dispatch_unconfirmed":
                return row
            match = await self.gh.reconcile(row)
            if not match:
                if time.time() - row["dispatch_at"] > 600:
                    return self.terminal(row, "dispatch_unconfirmed")
                return self.store.update(row, next_check=time.time() + 3)
            row = self.store.update(row, status="queued", error=None, next_check=0, **match)
            if not row["run_id"]:
                return row
        run = await self.gh.get(row)
        if run is None:
            if row["status"] == "completed" and row["manifest"]:
                try:
                    await self.b2.verify(row, self.cfg.prefix, json.loads(row["manifest"]))
                except Issue as exc:
                    if exc.code in ("output_invalid", "output_missing"):
                        return self.terminal(row, exc.code)
                    raise
                return row  # GitHub can expire run history before B2 expires the output.
            if row["status"] == "failed":
                return row
            if time.time() - row["dispatch_at"] < 90:
                raise Issue("remote_unavailable", 5)
            return self.terminal(row, "run_missing")
        attempt = run["run_attempt"]
        if row["attempt"] and attempt < row["attempt"]:
            raise Issue("remote_unavailable", 3)
        if row["attempt"] != attempt:
            # Persist the attempt before any B2 read. A timeout or restart must never
            # allow the previous attempt's verified document to satisfy this attempt.
            row = self.store.update(
                row, attempt=attempt, status="queued", manifest=None, error=None, next_check=0
            )
            if row["attempt"] != attempt:
                return row
        if run["status"] != "completed":
            state = "running" if run["status"] == "in_progress" else "queued"
            return self.store.update(
                row,
                status=state,
                attempt=attempt,
                manifest=None,
                error=None,
                next_check=time.time() + 2,
            )
        view = {**row, "attempt": attempt}
        conclusion = run.get("conclusion")
        if conclusion != "success":
            code = {"cancelled": "cancelled", "timed_out": "timed_out"}.get(conclusion, "worker_failed")
            if conclusion == "failure":
                # Make the GitHub failure durable even if the process exits or B2 is slow.
                if row["status"] != "failed" or row["attempt"] != attempt:
                    row = self.store.update(
                        row, status="failed", attempt=attempt, error=code, manifest=None
                    )
                    if row["attempt"] != attempt or row["status"] != "failed":
                        return row
                code = await self.b2.worker_error(view, self.cfg.prefix)
            return self.store.update(
                row, status="failed", attempt=attempt, error=code, manifest=None, next_check=0
            )
        try:
            manifest = await self.b2.verify(view, self.cfg.prefix)
        except Issue as exc:
            if exc.code in ("output_invalid", "output_missing"):
                return self.store.update(
                    row, status="failed", error=exc.code, attempt=attempt, manifest=None, next_check=0
                )
            raise
        return self.store.update(
            row,
            status="completed",
            attempt=attempt,
            error=None,
            manifest=json.dumps(manifest),
            next_check=0,
        )

    def render(self, row):
        identity = {"job_id": row["job_id"]}
        if row["run_id"]:
            identity["workflow_run_id"] = row["run_id"]
        if row["attempt"]:
            identity["run_attempt"] = row["attempt"]
        if row["status"] == "failed":
            return failure(row["error"], **identity)
        if row["status"] == "completed":
            manifest = json.loads(row["manifest"])
            url, expires = self.b2.sign(manifest["object_key"], self.cfg.ttl)
            return {
                **manifest,
                **identity,
                "status": "completed",
                "download_url": url,
                "expires_at": expires,
            }
        return pending(row, max(1, row["next_check"] - time.time()), row["error"])

    async def check(self, job_id):
        row = self.store.get(job_id)
        if row["target"] != self.cfg.target:
            raise Issue("configuration")
        if row["next_check"] > time.time():
            return pending(row, row["next_check"] - time.time(), row["error"])
        try:
            return self.render(await self.observe(row))
        except Issue as exc:
            if exc.code == "multiple_runs":
                return self.render(self.terminal(row, exc.code))
            if exc.code in ("rate_limited", "remote_unavailable", "github_access", "storage_access"):
                delay = exc.delay or 15
                row = self.store.get(job_id)  # observe may already have committed the new attempt.
                changes = {"next_check": time.time() + delay}
                if row["status"] not in ("completed", "failed"):
                    changes["error"] = exc.code
                row = self.store.update(row, **changes)
                return pending(row, delay, exc.code)
            raise

    async def watch(self, job_id, seconds, deadline):
        pause = 2
        while True:
            reply = await self.check(job_id)
            if not seconds or reply["status"] in ("failed", "completed"):
                return reply
            delay = max(reply["retry_after_seconds"], random.uniform(0.8, 1.2) * pause)
            if time.monotonic() + delay >= deadline:
                return reply
            await asyncio.sleep(delay)
            pause = min(15, pause * 1.6)

    def deadline(self, seconds):
        budget = self.cfg.deadline - 1
        if seconds > 0 and self.cfg.wait > 0:
            budget = min(budget, self.cfg.wait, seconds)
        return time.monotonic() + budget

    def after_timeout(self, job_id):
        row = self.store.get(job_id)
        if row["status"] == "failed":
            return self.render(row)
        return pending(row)

    async def get_document_result(self, job_id, wait_seconds=0):
        try:
            req = Lookup(job_id=job_id, wait_seconds=wait_seconds)
            end = self.deadline(req.wait_seconds)
            async with asyncio.timeout(end - time.monotonic()):
                return await self.watch(job_id, min(req.wait_seconds, self.cfg.wait), end)
        except TimeoutError:
            return self.after_timeout(job_id)
        except ValidationError:
            return problem("invalid_input")
        except Issue as exc:
            return problem(exc.code, job_id=job_id)

    async def generate_document(self, outline, format, wait_seconds=0, request_id=None):
        row = None
        try:
            req = Submit(
                outline=outline, format=format, wait_seconds=wait_seconds, request_id=request_id
            )
            end = self.deadline(req.wait_seconds)
            async with asyncio.timeout(end - time.monotonic()):
                try:
                    row, new = self.store.reserve(req)
                except Issue as exc:
                    if exc.code != "busy":
                        raise
                    for job in self.store.active_jobs():
                        await self.check(job)  # Reclaim jobs that ended while the agent was offline.
                    row, new = self.store.reserve(req)
                if new or row["status"] == "staging":
                    reply = await self.check(row["job_id"])
                    if not req.wait_seconds or reply["status"] == "failed":
                        return reply
                return await self.watch(row["job_id"], min(req.wait_seconds, self.cfg.wait), end)
        except TimeoutError:
            return self.after_timeout(row["job_id"]) if row else problem("busy")
        except ValidationError:
            return problem("invalid_input")
        except Issue as exc:
            reply = problem(exc.code)
            if exc.delay:
                reply["retry_after_seconds"] = exc.delay
            return reply


async def invoke(name, **args):
    gh = None
    try:
        cfg = Settings.load()
        if not setting("GITHUB_TOKEN"):
            raise Issue("configuration")
        b2 = B2(cfg.region, cfg.bucket, *credential("OUTPUT_READ"))
        gh = GitHub(cfg, setting("GITHUB_TOKEN"))
        requests = (
            B2(cfg.region, cfg.bucket, *credential("REQUEST_WRITE"))
            if name == "generate_document"
            else None
        )
        store = Store(cfg)
        agent = Agent(cfg, store, gh, b2, requests)
        if name == "generate_document":
            return await agent.generate_document(**args)
        if name == "get_document_result":
            return await agent.get_document_result(**args)
        return problem("invalid_input")
    except Issue as exc:
        return problem(exc.code)
    except (OSError, sqlite3.Error):
        return problem("configuration")
    finally:
        if gh:
            await gh.http.aclose()
